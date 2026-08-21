"""A node's fix-loop budget survives the run boundary it used to be reset by.

`execution.semantic_ceiling` is enforced by
`Scheduler._semantic_ceiling_reached`, which counts
`retry_policy.semantic_attempts_total` over `(run_id, node_id)`.
Inside a run that scope is right. Across runs it is a hole: `run start` mints a
fresh `run_id`, `create_run` seeds a `node_lifecycle` row with no history, and
the same node against the same plan bytes is handed the whole ceiling again.
A node that cannot be made to pass can therefore be re-attempted without limit
— one run at a time, with nothing in the ledger counting it and no operator
told. That is #92's shape: a debt no amount of spending pays off, and the
measured shape of `lane-p5-gap-policy`: 39 attempts over four `run_id`s.

The guard counted **review rejections** until §19 M35, which was the right
predicate for exactly as long as a rejection was the failure that repeated.
Review no longer fails an attempt, so what repeats is a content failure, and
every one of those is a SEMANTIC row.

What is settled here:

  §7.5   the cumulative count is `semantic_attempts_total` summed per run,
         never a second copy of the predicate (RC1)
  §7.1   a node's identity across runs is `(plan_digest, node_id)`; a re-shipped
         plan is different bytes and legitimately starts the count again
  §19 M6 the guard sits in `_execute_run`, so `run start` and `run resume` both
         cross it and neither can reach around the other
  §11.3  an operator grant standing on a prior run widens the ceiling it was
         given to widen, rather than being revoked by the next `run start`
  §3.6 B10  the refusal has an operator escape, and the escape leaves a typed
         record in the ledger rather than living in the argv of a dead process
  §1.2   every fact a caller branches on travels as a typed field

Every failure below is written through the real `LifecycleStore` by the same
`fail_attempt` call the scheduler makes, so the count is taken over rows a
scheduler would actually have written. Nothing here reads pane text,
prompt text, or any free-text field.

Run with:  uv run adws/adw_test.py -k cross_run_node_budget
"""

from __future__ import annotations

import contextlib
import io
import json
import os
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

from adw_modules import finalization as fin  # noqa: E402
from adw_modules import lifecycle as lc  # noqa: E402
from adw_modules import plan_canonical as pc  # noqa: E402
from adw_modules import receipt_crypto  # noqa: E402
from adw_modules import retry_policy as rp  # noqa: E402
from adw_modules import review_convergence as rc  # noqa: E402
from adw_modules import scheduler_types as st  # noqa: E402

from test_node_instruction_requirement_text import (  # noqa: E402
    _authored_plan, _ir, _repo)

BASE = "a" * 40

FIRST = "lane-p4-enrichment-ordering"
SECOND = "lane-p4-audit"

#: The outcome under test, spelled once. A literal repeated per assertion is
#: how a rename leaves half the suite asserting a name nothing emits.
OUTCOME = "NODE_BUDGET_EXHAUSTED_ACROSS_RUNS"

#: `_validate_run_paths` is the first thing `_execute_run` does *after* the
#: guard. A run that the cross-run budget admits would go on to resolve
#: runners, open the ledger for writing, take the integration branch and start
#: a scheduler, so the positive controls stop it there — everything up to and
#: including the guard is the real production path, and the outcome the test
#: reads back says which of the two happened.
HALTED = "TEST_HALTED_AFTER_THE_BUDGET_GUARD"


def _two_lane_ir() -> dict:
    """The real ingress fixture's IR, with a second independent lane.

    Two lanes rather than one because the refusal names a *set* of nodes and
    sorts it, and a single-node plan cannot tell a sorted list from an
    accidental one.
    """
    ir = _ir()
    ir["requirements"].append({
        "requirement_id": "req-p4-ordering-audit",
        "text": "Record every admission decision with its deciding clause, "
                "so an operator reading the log can say which clause "
                "admitted a document without re-running the pipeline.",
        "surface": [{"path": "src/audit.py", "mutation": "written"}],
        "effects": [],
    })
    ir["lanes"].append({
        "lane_id": SECOND,
        "title": "Audit the admission decisions",
        "execution_context": ".",
        "requirement_ids": ["req-p4-ordering-audit"],
        "depends_on": [],
        "verifier_ids": ["verify-p4-audit"],
    })
    ir["verifiers"].append({
        "verifier_id": "verify-p4-audit",
        "lane_ids": [SECOND],
        "source_ids": ["src-readme"],
        "command": "python3 -m pytest tests/test_audit.py",
        "min_executed": 1,
    })
    ir["extensions"]["maestro"]["outputs"][SECOND] = ["src/audit.py"]
    return ir


class CrossRunBudgetFixture(unittest.TestCase):
    """A configured repository, a real shipped plan, a real PASS receipt, and
    a real ledger — the four things `run start` needs before it can be refused
    for anything as specific as a budget."""

    SEMANTIC_CEILING = 3

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.repo = _repo(self.root)
        self.plan = _authored_plan(self.root, self.repo, _two_lane_ir())
        self.stored = pc.canonicalize(self.plan)
        self.digest = maestro.plan_digest.digest_of(self.stored)
        self.other_digest = "b" * 64
        self.nodes = list(self.plan.to_plan_nodes())
        self.state = (self.root / "maestro-state" / "repo").resolve()
        self._install_repository()
        self._install_receipt()

    # ── the installed repository ────────────────────────────────────────────

    def _install_repository(self):
        plan_file = self.repo / "plans" / "named" / "maestro-plan.v1"
        plan_file.parent.mkdir(parents=True, exist_ok=True)
        plan_file.write_bytes(self.stored)
        (self.repo / "adws").mkdir(exist_ok=True)
        binaries = {}
        for name in ("herdr", "omp", "claude"):
            binary = self.root / name
            binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            binary.chmod(0o755)
            binaries[name] = str(binary)
        route_dir = self.state / "route-receipts"
        route_dir.mkdir(parents=True, exist_ok=True)
        for route in ("omp", "claude"):
            (route_dir / (route + ".json")).write_text("{}", encoding="utf-8")
        self.seed = receipt_crypto.generate_seed()
        route_seed = receipt_crypto.generate_seed()
        self.environment = {
            "MAESTRO_TEST_VERIFY_KEY":
                receipt_crypto.seed_to_public_key(self.seed).hex(),
            "MAESTRO_TEST_SIGNING_SEED": self.seed.hex(),
            "MAESTRO_TEST_ROUTE_VERIFY_KEY":
                receipt_crypto.seed_to_public_key(route_seed).hex(),
        }
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
            "route_receipts": {"omp": "route-receipts/omp.json",
                               "claude": "route-receipts/claude.json"},
            "reviewer": {"route": "claude", "model": "review-model",
                         "effort": "high", "finalization_timeout_s": 60,
                         "turn_timeout_s": 20, "poll_interval_s": 1,
                         "vendor": "anthropic"},
            "execution": {"route": "omp", "model": "execution-model",
                          "effort": "medium", "concurrency": 2,
                          "node_timeout_s": 120, "turn_timeout_s": 30,
                          "final_acceptance_timeout_s": 45,
                          "backstop_t_s": 600,
                          "semantic_ceiling": self.SEMANTIC_CEILING,
                          "review_ceiling": 3,
                          "vendor": "openai"},
        }
        (self.repo / "adws" / "maestro.config.yaml").write_text(
            json.dumps(config), encoding="utf-8")

    def _install_receipt(self):
        """A real PASS receipt for the real digest, signed with the key the
        configuration names. Without it `_load_runnable_plan` refuses
        `RUN_RECEIPT_ABSENT` and no run reaches the guard at all."""
        store = fin.ReceiptStore(
            self.state / "receipts", repo_paths=(self.repo,),
            data_dir=self.state / "data",
            verify_keys=[receipt_crypto.seed_to_public_key(self.seed)],
            signing_seed=self.seed, create=True)
        # The receipt `plan finalize` writes since the reviewer was removed:
        # deterministic, PASS, no cells -- the production helper, so this
        # fixture cannot drift from what the verb actually signs.
        store.write(maestro._deterministic_receipt(self.digest))

    @property
    def database(self) -> Path:
        return self.state / "lifecycle.sqlite3"

    # ── driving the real verbs ──────────────────────────────────────────────

    @contextlib.contextmanager
    def _store(self):
        store = lc.LifecycleStore(self.database)
        try:
            yield store
        finally:
            store.close()

    def _run(self, argv):
        """`maestro.main` from inside the installed repository.

        `pv.validate_plan` is stubbed to say the bytes are canonical and
        eligible, which is the shape `test_plan_schema_version_admission` and
        `test_run_refusal_vocabulary` both use: the plan validator runs gate
        collection against a real toolchain, and it is not the subject here.
        Everything else on the path — configuration binding, digest
        derivation, receipt verification, plan parsing, and the guard itself —
        is the production code an operator reaches.
        """
        output = io.StringIO()
        previous = Path.cwd()
        os.chdir(self.repo)
        validation = mock.Mock(eligible=True, digest=self.digest)
        try:
            with mock.patch.dict(os.environ, self.environment, clear=False), \
                    mock.patch.object(maestro.pv, "validate_plan",
                                      return_value=validation), \
                    mock.patch.object(maestro, "_plan_collector",
                                      return_value=None), \
                    mock.patch.object(
                        maestro, "_validate_run_paths",
                        side_effect=maestro._RunRefused(
                            HALTED, "the guard admitted this run")), \
                    contextlib.redirect_stdout(output):
                code = maestro.main(argv)
        finally:
            os.chdir(previous)
        return code, json.loads(output.getvalue())

    # ── ledger builders, through the production store ───────────────────────

    @staticmethod
    def _reject(store, run_id, node_id, findings, *, blocked=False):
        """One rejected review, written exactly as `_settle_review_rejection`.

        Both halves in one call: the marker the review budget is counted over,
        and the transition detail carrying the check ids. The digest binds
        them, so the count can be recovered from either side.
        """
        lifecycle = store.get_node(run_id, node_id)
        digest = "{}-a{}".format(node_id, lifecycle.attempt_no)
        marker = {rp.REVIEW_REJECTED_KEY: True,
                  rc.REVIEW_SUBJECT_DIGEST_KEY: digest}
        detail = {"reason": "code review rejected the diff",
                  "subject_digest": digest, "replayed": False,
                  "blocking_checks": ["diff.check_{}".format(index)
                                      for index in range(findings)]}
        if blocked:
            store.mark_blocked(
                run_id, node_id, st.BlockReason.SEMANTIC_BUDGET_EXHAUSTED,
                detail=detail, retry_class=st.RetryClass.SEMANTIC,
                attempt_extra=marker)
        else:
            store.fail_attempt(run_id, node_id, st.RetryClass.SEMANTIC,
                               detail=detail, attempt_extra=marker)

    def _rejected_run(self, run_id, rejections, *, node_id=FIRST,
                      digest=None, blocked=False):
        """A run of this plan that rejected one node `rejections` times.

        `blocked` ends it the way a run that spent its ceiling actually ends:
        the last rejection is written by `mark_blocked` rather than
        `fail_attempt` — the same marker on the same attempt, so the count is
        unchanged — and the scheduler then declares the run's outcome. That is
        what makes an operator escape legal against it (§11.2).
        """
        with self._store() as store:
            store.create_run(run_id, digest or self.digest, self.nodes)
            for index in range(rejections):
                store.start_attempt(run_id, node_id, BASE)
                self._reject(store, run_id, node_id, 3 - min(index, 2),
                             blocked=blocked and index == rejections - 1)
            if blocked:
                store.declare_outcome(run_id)
        return run_id

    def _grant(self, run_id, node_id, amount):
        with self._store() as store:
            store.retry(run_id, node_id, grant=amount)


# ── the refusal, through `run start` ────────────────────────────────────────

class TheCrossRunBudgetRefusesAFreshRunTest(CrossRunBudgetFixture):

    def test_two_prior_runs_over_the_ceiling_refuse_the_start(self):
        self._rejected_run("run-aaa-first", 3)
        self._rejected_run("run-bbb-second", 2)

        code, payload = self._run(["run", "start", "named"])

        self.assertEqual(3, code)
        self.assertEqual(OUTCOME, payload["outcome"])

    def test_the_refusal_carries_the_node_the_count_and_the_runs(self):
        """§1.2 — an operator tool deciding which nodes to re-author branches
        on these, not on the sentence beside them."""
        self._rejected_run("run-aaa-first", 3)
        self._rejected_run("run-bbb-second", 2)

        _code, payload = self._run(["run", "start", "named"])

        self.assertEqual(self.digest, payload["plan_digest"])
        self.assertEqual(self.SEMANTIC_CEILING, payload["semantic_ceiling"])
        self.assertEqual([{"node_id": FIRST,
                           "cumulative_semantic_attempts": 5,
                           "effective_ceiling": 3,
                           "run_ids": ["run-bbb-second", "run-aaa-first"]}],
                         payload["nodes"])

    def test_the_detail_names_the_remedy(self):
        """A refusal an operator cannot act on sends them to the source. The
        escape is the only way past this one, so the sentence has to say it."""
        self._rejected_run("run-aaa-first", 4)

        _code, payload = self._run(["run", "start", "named"])

        self.assertIn(FIRST, payload["detail"])
        self.assertIn("--allow-exhausted-node", payload["detail"])
        self.assertIn("run convergence", payload["detail"])

    def test_the_nodes_are_sorted_by_node_id(self):
        """Deterministic output, because two nodes over the ceiling must
        produce one document rather than one per dictionary ordering."""
        self._rejected_run("run-aaa-first", 4)
        self._rejected_run("run-bbb-second", 4, node_id=SECOND)

        _code, payload = self._run(["run", "start", "named"])

        self.assertEqual([SECOND, FIRST],
                         [entry["node_id"] for entry in payload["nodes"]])
        self.assertEqual(sorted(entry["node_id"] for entry in payload["nodes"]),
                         [entry["node_id"] for entry in payload["nodes"]])

    def test_exactly_at_the_ceiling_is_not_yet_exceeded(self):
        """`exceed` means strictly greater. A node that spent its ceiling and
        no more is a node the in-run guard has already stopped once; refusing
        it here as well would move the boundary by one without saying so."""
        self._rejected_run("run-aaa-first", self.SEMANTIC_CEILING)

        code, payload = self._run(["run", "start", "named"])

        self.assertEqual(HALTED, payload["outcome"], payload)
        self.assertEqual(3, code)


# ── the positive control, and the two scopes ────────────────────────────────

class TheGuardRefusesOnlyWhatItShouldTest(CrossRunBudgetFixture):

    def test_prior_runs_under_the_ceiling_do_not_refuse(self):
        """Without this the guard could be refusing every run and every
        assertion above it would still pass (§13.4)."""
        self._rejected_run("run-aaa-first", 1)
        self._rejected_run("run-bbb-second", 1)

        _code, payload = self._run(["run", "start", "named"])

        self.assertEqual(HALTED, payload["outcome"], payload)

    def test_a_run_with_no_prior_runs_at_all_does_not_refuse(self):
        _code, payload = self._run(["run", "start", "named"])

        self.assertEqual(HALTED, payload["outcome"], payload)

    def test_rejections_under_another_digest_do_not_count(self):
        """§7.1 — the node's identity across runs is `(plan_digest, node_id)`.
        A lane id is not unique across plans, and counting one plan's
        rejections against another's node would refuse a run for work it never
        did."""
        self._rejected_run("run-aaa-other", 6, digest=self.other_digest)

        _code, payload = self._run(["run", "start", "named"])

        self.assertEqual(HALTED, payload["outcome"], payload)

    def test_a_prior_run_grant_widens_the_ceiling(self):
        """§11.3 — `retry --grant N` is a deliberate operator act, and it is
        recorded on the run it was typed against. Ignoring it here would let
        the next `run start` silently revoke a widening an operator made
        precisely to absorb these rejections."""
        run_id = self._rejected_run("run-aaa-first", 5, blocked=True)
        self._grant(run_id, FIRST, 3)

        _code, payload = self._run(["run", "start", "named"])

        self.assertEqual(HALTED, payload["outcome"], payload)

    def test_a_grant_too_small_to_cover_the_spend_still_refuses(self):
        """The grant widens the ceiling; it does not disable the rule."""
        run_id = self._rejected_run("run-aaa-first", 6, blocked=True)
        self._grant(run_id, FIRST, 1)

        _code, payload = self._run(["run", "start", "named"])

        self.assertEqual(OUTCOME, payload["outcome"])
        self.assertEqual(4, payload["nodes"][0]["effective_ceiling"])
        self.assertEqual(6, payload["nodes"][0]["cumulative_semantic_attempts"])


# ── `run resume` crosses the same guard (§19 M6) ────────────────────────────

class BothRunVerbsCrossTheGuardTest(CrossRunBudgetFixture):

    def test_resume_does_not_charge_a_run_for_its_own_rejections(self):
        """The run being resumed is already governed by
        `Scheduler._semantic_ceiling_reached`, which counts exactly these rows.
        Counting them here as well would charge one rejection to two budgets
        and make every over-ceiling run unresumable — turning the escape from
        a blocked node into a dead end."""
        self._rejected_run("run-aaa-only", 6)

        _code, payload = self._run(["run", "resume", "named"])

        self.assertEqual(HALTED, payload["outcome"], payload)

    def test_resume_is_refused_when_an_earlier_run_exceeded_it(self):
        """M6's route. A guard on `run start` alone is reached around by
        `run resume`, which enters the same `_execute_run`."""
        self._rejected_run("run-aaa-first", 6)
        self._rejected_run("run-bbb-resumed", 1)

        code, payload = self._run(["run", "resume", "named"])

        self.assertEqual(3, code)
        self.assertEqual(OUTCOME, payload["outcome"])
        self.assertEqual(["run-aaa-first"], payload["nodes"][0]["run_ids"])
        self.assertEqual(6, payload["nodes"][0][
            "cumulative_semantic_attempts"])


# ── §3.6 B10's escape ───────────────────────────────────────────────────────

class TheOperatorEscapeTest(CrossRunBudgetFixture):

    def test_the_named_node_is_admitted(self):
        self._rejected_run("run-aaa-first", 6)

        _code, payload = self._run(
            ["run", "start", "named", "--allow-exhausted-node", FIRST])

        self.assertEqual(HALTED, payload["outcome"], payload)

    def test_its_use_is_recorded_as_a_typed_fact_in_the_ledger(self):
        """§1.2 — otherwise the whole of "an operator allowed this" is the
        argv of a process that has exited, and the run's ledger shows a node
        with more attempts than the ceiling permits and nothing saying why."""
        self._rejected_run("run-aaa-first", 6)

        self._run(["run", "start", "named", "--allow-exhausted-node", FIRST])

        reader = lc.LifecycleReader.open(self.database)
        try:
            rows = [row for run in reader.runs()
                    for row in reader.transitions(run.run_id)
                    if row["reason"] == lc.NODE_BUDGET_ALLOWANCE_REASON]
            # The row is filed under the run id this invocation was about to
            # use, which is not one of the runs already in the ledger.
            fresh = [row for row in reader.conn.execute(
                "SELECT run_id, node_id, actor, detail_json FROM transitions"
                " WHERE reason=?", (lc.NODE_BUDGET_ALLOWANCE_REASON,))]
        finally:
            reader.close()
        self.assertEqual([], rows)
        self.assertEqual(1, len(fresh))
        run_id, node_id, actor, detail = fresh[0]
        self.assertTrue(run_id.startswith("run-"))
        self.assertNotIn(run_id, ("run-aaa-first",))
        self.assertEqual(FIRST, node_id)
        self.assertEqual("operator", actor)
        self.assertEqual({"cumulative_semantic_attempts": 6,
                          "effective_ceiling": 3,
                          "run_ids": ["run-aaa-first"]},
                         json.loads(detail))

    def test_an_unnamed_node_is_still_refused(self):
        """The escape admits the node it names and nothing else."""
        self._rejected_run("run-aaa-first", 6)
        self._rejected_run("run-bbb-second", 6, node_id=SECOND)

        _code, payload = self._run(
            ["run", "start", "named", "--allow-exhausted-node", FIRST])

        self.assertEqual(OUTCOME, payload["outcome"])
        self.assertEqual([SECOND],
                         [entry["node_id"] for entry in payload["nodes"]])

    def test_nothing_is_recorded_when_the_run_is_refused_anyway(self):
        """The record says an operator's allowance took effect. A run that was
        refused for a different node ran nothing, so an allowance row there
        would attest to an admission that never happened."""
        self._rejected_run("run-aaa-first", 6)
        self._rejected_run("run-bbb-second", 6, node_id=SECOND)

        self._run(["run", "start", "named", "--allow-exhausted-node", FIRST])

        reader = lc.LifecycleReader.open(self.database)
        try:
            rows = list(reader.conn.execute(
                "SELECT 1 FROM transitions WHERE reason=?",
                (lc.NODE_BUDGET_ALLOWANCE_REASON,)))
        finally:
            reader.close()
        self.assertEqual([], rows)

    def test_a_node_id_the_plan_does_not_contain_is_refused(self):
        """A misspelled node id admits nothing and refuses nothing, so it is
        refused here rather than silently ignored — the shape `--grant 0` is
        refused at parse time for."""
        self._rejected_run("run-aaa-first", 6)

        code, payload = self._run(
            ["run", "start", "named", "--allow-exhausted-node", "lane-typo"])

        self.assertEqual(3, code)
        self.assertEqual("ALLOW_EXHAUSTED_NODE_UNKNOWN", payload["outcome"])
        self.assertEqual(["lane-typo"], payload["unknown_node_ids"])
        self.assertEqual(sorted((FIRST, SECOND)), payload["plan_node_ids"])

    def test_the_escape_survives_the_configured_run_flag_rule(self):
        """A configured named-plan run verb accepts no runtime flags, and
        `--allow-exhausted-node` had to be exempted or B10's escape would be
        unreachable on every installed repository: refused outright on
        `run start`, and on `run resume` it would switch configuration binding
        off and demand fifteen paths by hand (#91)."""
        self._rejected_run("run-aaa-first", 6)

        _code, payload = self._run(
            ["run", "resume", "named", "--allow-exhausted-node", FIRST])

        self.assertNotEqual("MAESTRO_CONFIGURATION_INVALID",
                            payload["outcome"])
        self.assertEqual(HALTED, payload["outcome"], payload)


# ── the counter itself ──────────────────────────────────────────────────────

class TheCumulativeCountIsOnePredicateTest(unittest.TestCase):
    """`semantic_attempts_across_runs` as a pure function over attempt rows.

    RC1: a second copy of a budget rule lived in
    `retry_policy.review_budget_exhausted`, had no production caller, and
    disagreed with the enforced one by exactly one attempt. This function sums
    `semantic_attempts_total` rather than restating its predicate, and these
    cases pin that it agrees with it row for row.
    """

    @staticmethod
    def _attempt(run_id, node_id, attempt_no, *, rejected, semantic=True):
        return st.AttemptRecord(
            run_id=run_id, node_id=node_id, attempt_no=attempt_no,
            base_sha=BASE, state=st.NodeState.PENDING, started_at=0.0,
            launched_at=None, pid=None, turn_count=0,
            retry_class=(st.RetryClass.SEMANTIC if semantic
                         else st.RetryClass.ENVIRONMENTAL),
            extra=({rp.REVIEW_REJECTED_KEY: True} if rejected else {}))

    def test_it_sums_the_per_run_count(self):
        rows = {
            "run-a": (self._attempt("run-a", "n", 1, rejected=True),
                      self._attempt("run-a", "n", 2, rejected=True)),
            "run-b": (self._attempt("run-b", "n", 1, rejected=True),),
        }
        self.assertEqual((3, ("run-a", "run-b")),
                         rp.semantic_attempts_across_runs(rows, "n"))

    def test_it_agrees_with_semantic_attempts_total_per_run(self):
        rows = {
            "run-a": (self._attempt("run-a", "n", 1, rejected=True),
                      self._attempt("run-a", "n", 2, rejected=False)),
            "run-b": (self._attempt("run-b", "n", 1, rejected=True),
                      self._attempt("run-b", "n", 2, rejected=True)),
        }
        total, _runs = rp.semantic_attempts_across_runs(rows, "n")
        self.assertEqual(
            sum(rp.semantic_attempts_total(attempts, "n")
                for attempts in rows.values()),
            total)

    def test_a_run_with_no_semantic_failure_is_not_named(self):
        """The run ids exist so the refusal can say where the attempts went.
        A run that holds none of them is not an answer to that question."""
        rows = {
            "run-a": (self._attempt("run-a", "n", 1, rejected=False,
                                    semantic=False),),
            "run-b": (self._attempt("run-b", "n", 1, rejected=True),),
        }
        self.assertEqual((1, ("run-b",)),
                         rp.semantic_attempts_across_runs(rows, "n"))

    def test_another_nodes_attempts_are_not_counted(self):
        rows = {"run-a": (self._attempt("run-a", "other", 1, rejected=True),)}
        self.assertEqual((0, ()), rp.semantic_attempts_across_runs(rows, "n"))

    def test_an_infra_failure_is_not_counted(self):
        """No infra fault ever produces a budget decrement (§7.5). An
        ENVIRONMENTAL row is invisible here whatever else it carries."""
        rows = {"run-a": (self._attempt("run-a", "n", 1, rejected=False,
                                        semantic=False),
                          self._attempt("run-a", "n", 2, rejected=True,
                                        semantic=False))}
        self.assertEqual((0, ()), rp.semantic_attempts_across_runs(rows, "n"))

    def test_a_content_failure_that_is_not_a_rejection_is_counted(self):
        """The exclusion §19 M35 removed, asserted as its absence: a red gate
        is a content failure and spends this budget, marker or no marker."""
        rows = {"run-a": (self._attempt("run-a", "n", 1, rejected=False),
                          self._attempt("run-a", "n", 2, rejected=False))}
        self.assertEqual((2, ("run-a",)),
                         rp.semantic_attempts_across_runs(rows, "n"))

    def test_no_runs_at_all_is_zero(self):
        self.assertEqual((0, ()), rp.semantic_attempts_across_runs({}, "n"))


# ── the ledger query ────────────────────────────────────────────────────────

class TheReaderScopesByDigestAndExcludesTheCurrentRunTest(
        CrossRunBudgetFixture):

    def test_it_returns_every_run_of_the_plan_but_the_excluded_one(self):
        self._rejected_run("run-aaa-first", 1)
        self._rejected_run("run-bbb-second", 1)
        self._rejected_run("run-ccc-other", 1, digest=self.other_digest)

        reader = lc.LifecycleReader.open(self.database)
        try:
            found = reader.attempts_by_run_for_plan(
                self.digest, exclude_run_id="run-bbb-second")
        finally:
            reader.close()

        self.assertEqual(["run-aaa-first"], list(found))
        self.assertEqual(1, len(found["run-aaa-first"]))

    def test_grants_are_summed_over_the_prior_runs(self):
        self._grant(
            self._rejected_run("run-aaa-first", 1, blocked=True), FIRST, 2)
        self._grant(
            self._rejected_run("run-bbb-second", 1, blocked=True), FIRST, 3)

        reader = lc.LifecycleReader.open(self.database)
        try:
            granted = reader.granted_extra_attempts_for_plan(self.digest)
            without = reader.granted_extra_attempts_for_plan(
                self.digest, exclude_run_id="run-bbb-second")
        finally:
            reader.close()

        self.assertEqual(5, granted[FIRST])
        self.assertEqual(0, granted[SECOND])
        self.assertEqual(2, without[FIRST])

    def test_the_reader_never_writes_the_ledger(self):
        """The guard runs before the run's own ledger exists, so it must not
        be the thing that creates it."""
        self._rejected_run("run-aaa-first", 1)
        before = self.database.stat().st_mtime_ns

        reader = lc.LifecycleReader.open(self.database)
        try:
            reader.attempts_by_run_for_plan(self.digest)
            reader.granted_extra_attempts_for_plan(self.digest)
        finally:
            reader.close()

        self.assertEqual(before, self.database.stat().st_mtime_ns)


if __name__ == "__main__":
    unittest.main()
