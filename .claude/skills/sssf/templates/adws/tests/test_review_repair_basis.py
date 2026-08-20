"""A rejected diff is repaired, not re-implemented.

`lane-p5-gap-policy` of run fb9973646d344400a9e4f4d7818d00f2 was rejected by
code review four times and produced 2, 2, 1, 3 findings across those four
attempts. The ledger says why it could not converge: every one of the five
attempt rows carried the **same** `base_sha`. A review rejection discarded the
rejected diff and the next attempt started from the node's base — an empty
`gap_policy.py` — rewriting all 573 lines from scratch, so consecutive attempts
were not iterations of one artifact but independent implementations, each
judged fresh. Findings could not descend monotonically because nothing
accumulated, and attempt 3's one-finding version was deleted rather than
repaired. `review_ceiling` was six chances to guess right in one shot.

This is issue #90's repair applied to the other actor. There a *reviewer* that
stalled was re-dispatched against the attempt's surviving output commit rather
than the builder being discarded; here a builder whose diff was rejected is
sent back to that same surviving commit rather than to an empty tree.

These tests hold that in place:

* a SEMANTIC **review rejection** opens the next attempt on the rejected
  attempt's output commit, and the node still merges;
* a SEMANTIC failure that is *not* a rejection — a red post-gate, a clause-4
  conviction — still restarts from the node's base, because that tree never
  passed its own gate and there is nothing to repair;
* an ENVIRONMENTAL or LAUNCHER_TRANSIENT retry is untouched;
* the repair prompt is not the implement prompt with findings appended: the
  node's instruction and the located findings both reach it, and it says in
  as many words that the work already exists and is to be changed rather
  than written;
* the chain breaks — at its length limit, and when a repair raises more
  findings than the rejection it repaired — and falls back to a fresh base
  rather than blocking;
* and a sibling merge that moves the integration head refuses the repair
  outright, so a repair can never hand a builder a tree missing merged work.

Everything is driven through the real `Scheduler` over a real git repository,
because the scheduler is where the defect lived: a test over `decide_repair`
alone would have passed against the broken build.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

import maestro  # noqa: E402
from adw_modules import lifecycle as lc  # noqa: E402
from adw_modules import retry_policy as rp  # noqa: E402
from adw_modules import scheduler as sch  # noqa: E402
from adw_modules import scheduler_types as st  # noqa: E402
from adw_modules import worktree as wt  # noqa: E402

from test_scheduler import SchedulerFixture, _git, green, red  # noqa: E402


class _Cell:
    """The three facts a graded cell contributes to guidance."""

    def __init__(self, check_id: str, object_id: str, message: str):
        self.check_id = check_id
        self.object_id = object_id
        self.message = message


class _Review:
    """A reviewer verdict, duck-typed as the scheduler consumes it."""

    def __init__(self, passed: bool, findings=(), digest: str = "digest"):
        self.passed = passed
        self.findings = list(findings)
        self.advisories: list = []
        self.subject_digest = digest
        self.replayed = False

    def findings_text(self) -> str:
        return "\n".join(
            "[{0}] {1}: {2}".format(f.object_id, f.check_id, f.message)
            for f in self.findings)


def reject(n: int, digest: str) -> _Review:
    """A rejection carrying `n` located findings."""
    return _Review(False, digest=digest, findings=[
        _Cell("diff.introduces_no_obvious_defect", "a.py",
              "finding {0} of {1}".format(i + 1, n))
        for i in range(n)])


class RepairFixture(SchedulerFixture):
    """One agent node, a scripted reviewer, and a builder that writes a
    distinguishable file on every attempt so a real delta exists each time."""

    def scripted_reviewer(self, reviews):
        """Pops one scripted verdict per dispatch and records its subject."""
        self.reviewed = []

        def review_attempt(attempt, node, record, base_sha, output_sha):
            self.reviewed.append((record.attempt_no, base_sha, output_sha))
            return reviews.pop(0)

        return review_attempt

    def per_attempt_builder(self):
        """A builder whose output differs per attempt.

        Writing identical bytes on a repair attempt would produce an empty
        measured delta, which proves nothing about whether the tree it started
        from was the rejected one.
        """
        def run_node(attempt, node, record, retry_prompt, on_launch,
                     cancel_requested):
            self.prompts.setdefault(node.node_id, []).append(retry_prompt)
            on_launch(None)
            (attempt.path / "a.py").write_text(
                "A{0}\n".format(record.attempt_no))
            return sch.NodeExecution(envelope_parsed=True, exit_code=0)

        return run_node

    def bases(self, node_id: str = "a"):
        return [row.base_sha
                for row in sorted(self.store.attempts_for("run1", node_id),
                                  key=lambda row: row.attempt_no)]

    def head(self) -> str:
        return wt.integration_head(self.repo, "integration/run1")

    def attempt_ref(self, attempt_no: int, node_id: str = "a"):
        return wt.attempt_ref_commit(self.repo, "run1", node_id, attempt_no)


# ── the base-selection rule ─────────────────────────────────────────────────

class RepairBaseSelectionTests(RepairFixture):

    def test_a_review_rejection_bases_the_next_attempt_on_the_rejected_diff(self):
        """The regression itself.

        Before the repair, attempts 1 and 2 carried the same `base_sha` and
        attempt 2 started from an empty tree. The assertion that goes red if a
        rejection ever again discards a committed builder attempt.
        """
        reviews = [reject(2, "d1"), _Review(True, digest="d2")]
        report = self.schedule(
            [self.agent("a")],
            config=self.config(review_ceiling=4),
            deps=self.deps(run_node=self.per_attempt_builder(),
                           review_attempt=self.scripted_reviewer(reviews))
        ).run()

        self.assertIs(report.outcome, st.RunOutcome.ACCEPTED)
        self.assertIs(self.store.get_node("run1", "a").state,
                      st.NodeState.MERGED)

        rows = sorted(self.store.attempts_for("run1", "a"),
                      key=lambda row: row.attempt_no)
        self.assertEqual([row.attempt_no for row in rows], [1, 2])
        rejected_sha = rows[0].extra[rp.REVIEW_OUTPUT_SHA_KEY]

        # The typed fact: the second attempt's recorded base is the first
        # attempt's output commit, and is not the node's base.
        self.assertEqual(rows[1].base_sha, rejected_sha)
        self.assertNotEqual(rows[1].base_sha, rows[0].base_sha)
        # And that sha is what the first attempt's own durable ref publishes.
        self.assertEqual(self.attempt_ref(1), rejected_sha)

    def test_the_repair_attempt_starts_from_the_rejected_tree_on_disk(self):
        """Not merely the recorded base — the tree the builder actually sees.

        A recorded `base_sha` that the worktree did not branch from would be
        the same defect with a correct-looking ledger.
        """
        seen = {}
        reviews = [reject(1, "d1"), _Review(True, digest="d2")]

        def run_node(attempt, node, record, retry_prompt, on_launch,
                     cancel_requested):
            self.prompts.setdefault(node.node_id, []).append(retry_prompt)
            on_launch(None)
            target = attempt.path / "a.py"
            seen[record.attempt_no] = (
                target.read_text() if target.exists() else None)
            target.write_text("A{0}\n".format(record.attempt_no))
            return sch.NodeExecution(envelope_parsed=True, exit_code=0)

        self.schedule(
            [self.agent("a")],
            config=self.config(review_ceiling=4),
            deps=self.deps(run_node=run_node,
                           review_attempt=self.scripted_reviewer(reviews))
        ).run()

        # Attempt 1 saw an empty tree; attempt 2 saw attempt 1's rejected work.
        self.assertIsNone(seen[1])
        self.assertEqual(seen[2], "A1\n")

    def test_the_reviewer_still_judges_the_whole_node_diff_from_the_head(self):
        """§8.1's other half. The repair changes what the *builder* starts
        from; it must not change what the *reviewer* is shown. A reviewer
        handed `rejected..output` would judge only the repair delta and could
        not see the node's work as a whole."""
        reviews = [reject(1, "d1"), _Review(True, digest="d2")]
        self.schedule(
            [self.agent("a")],
            config=self.config(review_ceiling=4),
            deps=self.deps(run_node=self.per_attempt_builder(),
                           review_attempt=self.scripted_reviewer(reviews))
        ).run()

        head = self.bases()[0]
        self.assertEqual([base for _no, base, _out in self.reviewed],
                         [head, head])

    def test_the_transition_records_why_the_base_was_chosen(self):
        """§3.6 B15 for the decision's own reason: the refusals leave no trace
        on the attempt row, because a refused repair produces an ordinary
        fresh-base attempt indistinguishable from any other. The audit tier is
        where the reason lands, and `transitions()` is its reader."""
        reviews = [reject(1, "d1"), _Review(True, digest="d2")]
        self.schedule(
            [self.agent("a")],
            config=self.config(review_ceiling=4),
            deps=self.deps(run_node=self.per_attempt_builder(),
                           review_attempt=self.scripted_reviewer(reviews))
        ).run()

        reader = lc.LifecycleReader.open(self.root / "lifecycle.db")
        self.addCleanup(reader.conn.close)
        reasons = [row.get("detail", {}).get("repair")
                   for row in reader.transitions("run1")
                   if row.get("node_id") == "a"
                   and row.get("reason") == "attempt-start"]
        self.assertEqual(reasons,
                         [rp.REPAIR_NO_PRIOR_REJECTION, rp.REPAIR_ADMITTED])

    def test_the_repair_marker_records_the_head_it_was_derived_from(self):
        """`base_sha` can no longer carry the integration head, so the row
        carries it explicitly — and `AttemptRecord.integration_head` reads it."""
        reviews = [reject(1, "d1"), _Review(True, digest="d2")]
        self.schedule(
            [self.agent("a")],
            config=self.config(review_ceiling=4),
            deps=self.deps(run_node=self.per_attempt_builder(),
                           review_attempt=self.scripted_reviewer(reviews))
        ).run()

        rows = sorted(self.store.attempts_for("run1", "a"),
                      key=lambda row: row.attempt_no)
        self.assertEqual(rows[1].integration_head, rows[0].base_sha)
        self.assertEqual(rows[1].repair_of_attempt, 1)
        self.assertEqual(rows[1].repair_chain_length, 1)
        # And the two attempts still share one guidance scope, so the repair
        # prompt is rendered from a ledger that holds the rejection.
        self.assertEqual(rows[0].guidance_key, rows[1].guidance_key)


# ── the failures that must NOT repair ───────────────────────────────────────

class NonReviewFailuresStillRestartTests(RepairFixture):

    def test_a_red_post_gate_still_restarts_from_the_node_base(self):
        """A SEMANTIC failure that is not a rejection. The tree it produced
        never passed its own gate, so there is nothing worth repairing."""
        self.written = {"a": {"a.py": "A\n"}}
        self.gate_script = {("a", "post"): [red(), green()]}
        report = self.schedule(
            [self.agent("a")], config=self.config(semantic_ceiling=3)).run()

        self.assertIs(report.outcome, st.RunOutcome.ACCEPTED)
        bases = self.bases()
        self.assertEqual(len(bases), 2)
        self.assertEqual(bases[0], bases[1])

    def test_a_clause_four_conviction_still_restarts_from_the_node_base(self):
        """The other SEMANTIC shape: a write outside the declared outputs,
        convicted before the commit exists. There is no output commit at all."""
        def run_node(attempt, node, record, retry_prompt, on_launch,
                     cancel_requested):
            self.prompts.setdefault(node.node_id, []).append(retry_prompt)
            on_launch(None)
            (attempt.path / "a.py").write_text("A\n")
            if record.attempt_no == 1:
                (attempt.path / "rogue.py").write_text("X\n")
            return sch.NodeExecution(envelope_parsed=True, exit_code=0)

        self.schedule([self.agent("a")], config=self.config(semantic_ceiling=3),
                      deps=self.deps(run_node=run_node)).run()

        bases = self.bases()
        self.assertEqual(len(bases), 2)
        self.assertEqual(bases[0], bases[1])

    def test_an_environmental_retry_is_unaffected(self):
        self.written = {"a": {"a.py": "A\n"}}
        self.raise_for = {"a": RuntimeError("the machine, not the code")}
        report = self.schedule([self.agent("a")]).run()

        self.assertIs(report.outcome, st.RunOutcome.ACCEPTED)
        rows = sorted(self.store.attempts_for("run1", "a"),
                      key=lambda row: row.attempt_no)
        self.assertEqual(len(rows), 2)
        self.assertIs(rows[0].retry_class, st.RetryClass.ENVIRONMENTAL)
        self.assertEqual(rows[0].base_sha, rows[1].base_sha)

    def test_a_launcher_transient_retry_is_unaffected(self):
        self.written = {"a": {"a.py": "A\n"}}
        self.raise_for = {"a": sch.LaunchFailed(rp.LauncherFailure.TRANSPORT)}
        report = self.schedule([self.agent("a")]).run()

        self.assertIs(report.outcome, st.RunOutcome.ACCEPTED)
        rows = sorted(self.store.attempts_for("run1", "a"),
                      key=lambda row: row.attempt_no)
        self.assertEqual(len(rows), 2)
        self.assertIs(rows[0].retry_class, st.RetryClass.LAUNCHER_TRANSIENT)
        self.assertEqual(rows[0].base_sha, rows[1].base_sha)


# ── §8.1: the integration head is allowed to move ───────────────────────────

class SiblingMergeTests(RepairFixture):

    def test_a_sibling_merge_refuses_the_repair_and_yields_a_fresh_tree(self):
        """The case that must never silently produce a stale tree.

        The rejected commit descends from the head the rejection was measured
        against. Once a sibling merges, that is no longer integration's head,
        and branching from the rejected commit would hand the builder a tree
        without the sibling's work. The repair is refused, the attempt takes
        the new head, and the sibling's file is present in its worktree.
        """
        reviews = [reject(1, "d1"), _Review(True, digest="d2")]
        trees = {}
        moved = {}

        def review_attempt(attempt, node, record, base_sha, output_sha):
            verdict = reviews.pop(0)
            if not verdict.passed:
                # A sibling lands on the integration branch between the
                # rejection and the next attempt — the real race, made
                # deterministic.
                (self.integration / "sibling.py").write_text("S\n")
                _git(self.integration, "add", "sibling.py")
                _git(self.integration, "commit", "-q", "-m", "sibling")
                moved["head"] = self.head()
            return verdict

        def run_node(attempt, node, record, retry_prompt, on_launch,
                     cancel_requested):
            self.prompts.setdefault(node.node_id, []).append(retry_prompt)
            on_launch(None)
            trees[record.attempt_no] = (attempt.path / "sibling.py").exists()
            (attempt.path / "a.py").write_text(
                "A{0}\n".format(record.attempt_no))
            return sch.NodeExecution(envelope_parsed=True, exit_code=0)

        self.schedule(
            [self.agent("a")],
            config=self.config(review_ceiling=4),
            deps=self.deps(run_node=run_node,
                           review_attempt=review_attempt)).run()

        rows = sorted(self.store.attempts_for("run1", "a"),
                      key=lambda row: row.attempt_no)
        self.assertEqual(len(rows), 2)
        # Not the rejected commit: the moved head.
        self.assertNotEqual(rows[1].base_sha,
                            rows[0].extra[rp.REVIEW_OUTPUT_SHA_KEY])
        self.assertEqual(rows[1].base_sha, moved["head"])
        # The sibling's work is in the tree the builder was handed, which is
        # the property a stale repair base would have destroyed.
        self.assertFalse(trees[1])
        self.assertTrue(trees[2])
        self.assertEqual(rows[1].repair_chain_length, 0)


# ── the chain-break rule ────────────────────────────────────────────────────

class ChainBreakTests(RepairFixture):

    def _run_rejections(self, verdicts, review_ceiling=8):
        self.schedule(
            [self.agent("a")],
            config=self.config(review_ceiling=review_ceiling),
            deps=self.deps(run_node=self.per_attempt_builder(),
                           review_attempt=self.scripted_reviewer(list(verdicts)))
        ).run()
        return sorted(self.store.attempts_for("run1", "a"),
                      key=lambda row: row.attempt_no)

    def test_the_chain_breaks_when_a_repair_raises_more_findings(self):
        """A repair that made the diff worse is the ledger saying the diff in
        hand is not the thing to keep. Two integers on rows the review budget
        already counts decide it — never the reviewer's prose."""
        rows = self._run_rejections([
            reject(1, "d1"),              # a1: fresh base, 1 finding
            reject(3, "d2"),              # a2: repairs a1, 3 findings — worse
            _Review(True, digest="d3"),   # a3: must be fresh
        ])
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[1].base_sha,
                         rows[0].extra[rp.REVIEW_OUTPUT_SHA_KEY])
        self.assertEqual(rows[2].base_sha, rows[0].base_sha)
        self.assertEqual(rows[2].repair_chain_length, 0)

    def test_a_flat_or_falling_findings_series_keeps_repairing(self):
        """The production series that could not converge was 2, 2, 1. Flat is
        not rising, so the chain survives it — otherwise the rule would break
        the very chains it exists to allow."""
        rows = self._run_rejections([
            reject(2, "d1"), reject(2, "d2"), reject(1, "d3"),
            _Review(True, digest="d4"),
        ])
        self.assertEqual(len(rows), 4)
        for index in (1, 2, 3):
            self.assertEqual(rows[index].base_sha,
                             rows[index - 1].extra[rp.REVIEW_OUTPUT_SHA_KEY])
        self.assertEqual([row.repair_chain_length for row in rows], [0, 1, 2, 3])

    def test_the_chain_breaks_at_its_length_limit(self):
        """Termination. `chain_length` strictly increases with every admitted
        repair and is compared against a constant, so no chain outlives the
        limit; a broken chain restarts at zero and the number of chains is
        bounded by the untouched `review_ceiling`."""
        self.assertEqual(rp.REPAIR_CHAIN_LIMIT, 3)
        rows = self._run_rejections([
            reject(2, "d1"), reject(2, "d2"), reject(2, "d3"), reject(2, "d4"),
            _Review(True, digest="d5"),
        ])
        self.assertEqual(len(rows), 5)
        self.assertEqual([row.repair_chain_length for row in rows],
                         [0, 1, 2, 3, 0])
        # The fifth attempt falls back to the node's base rather than blocking.
        self.assertEqual(rows[4].base_sha, rows[0].base_sha)
        self.assertIs(self.store.get_node("run1", "a").state,
                      st.NodeState.MERGED)

    def test_the_review_ceiling_still_ends_the_node(self):
        """The repair loop adds no attempts. Exhausting the review budget
        blocks exactly where it blocked before."""
        rows = self._run_rejections(
            [reject(2, "d{0}".format(i)) for i in range(1, 5)],
            review_ceiling=3)
        self.assertEqual(len(rows), 3)
        node = self.store.get_node("run1", "a")
        self.assertIs(node.state, st.NodeState.BLOCKED)
        self.assertIs(node.block_reason,
                      st.BlockReason.REVIEW_BUDGET_EXHAUSTED)


# ── the repair prompt ───────────────────────────────────────────────────────

class RepairPromptTests(RepairFixture):

    def test_the_repair_prompt_carries_the_instruction_and_the_findings(self):
        """A repair prompt is not the implement prompt with findings appended.

        Asserted over `maestro._agent_node_prompt`, which is the function that
        actually assembles what an agent node is sent: the node's instruction
        is its first line and still bounds the work, the located findings say
        what is wrong with the diff in hand, and the repair block says which
        of the two the agent is being asked to do.
        """
        reviews = [reject(2, "d1"), _Review(True, digest="d2")]
        self.schedule(
            [self.agent("a")],
            config=self.config(review_ceiling=4),
            deps=self.deps(run_node=self.per_attempt_builder(),
                           review_attempt=self.scripted_reviewer(reviews))
        ).run()

        prompts = self.prompts["a"]
        self.assertEqual(len(prompts), 2)
        self.assertIsNone(prompts[0])
        guidance = prompts[1]

        rows = sorted(self.store.attempts_for("run1", "a"),
                      key=lambda row: row.attempt_no)
        rejected_sha = rows[0].extra[rp.REVIEW_OUTPUT_SHA_KEY]

        # The difference between "write this" and "fix these in what you
        # wrote", stated rather than implied.
        self.assertIn("REPAIR, NOT REIMPLEMENTATION.", guidance)
        self.assertIn(rejected_sha, guidance)
        self.assertIn(rows[0].base_sha, guidance)
        self.assertIn("Do not write this node again from scratch",
                      guidance)
        # The located findings, both of them.
        self.assertIn("finding 1 of 2", guidance)
        self.assertIn("finding 2 of 2", guidance)

        node = self.agent("a")
        assembled = maestro._agent_node_prompt(
            node, Path("/tmp/envelope.json"), guidance)
        # Both bind, and the instruction comes first.
        self.assertTrue(assembled.startswith(node.instruction))
        self.assertIn("REPAIR, NOT REIMPLEMENTATION.", assembled)
        self.assertIn("finding 1 of 2", assembled)

    def test_a_fresh_base_prompt_never_claims_the_work_exists(self):
        """The counter-assertion. A retry that starts from an empty tree must
        not be told to repair something — the whole failure mode this change
        risks introducing is telling a builder its work is present when it is
        not."""
        self.written = {"a": {"a.py": "A\n"}}
        self.gate_script = {("a", "post"): [red(), green()]}
        self.schedule([self.agent("a")],
                      config=self.config(semantic_ceiling=3)).run()

        retry_prompt = self.prompts["a"][1]
        self.assertIsNotNone(retry_prompt)
        self.assertNotIn("REPAIR, NOT REIMPLEMENTATION.", retry_prompt)

    def test_the_repair_block_survives_a_budget_too_small_for_the_findings(self):
        """B13: a repair prompt whose repair instruction was elided is an
        implement prompt. The block renders before the surfaces divide the
        budget and is never what truncation drops."""
        ledger = rp.GuidanceLedger().with_review(rp.ReviewGuidance(
            subject_digest="d1",
            findings=tuple(
                rp.ReviewFinding("check-{0}".format(i), "a.py", "m" * 400, True)
                for i in range(40))))
        basis = rp.RepairBasis(base_sha="b" * 40, integration_head="c" * 40,
                               repair_of_attempt=1, chain_length=1)
        rendered = rp.render_guidance(
            self.agent("a"), ledger, char_budget=900, repair=basis)
        self.assertIn("REPAIR, NOT REIMPLEMENTATION.", rendered)
        self.assertIn("b" * 40, rendered)
        self.assertIn("Code review", rendered)
        self.assertIn("truncated", rendered)


# ── the decision, in isolation ──────────────────────────────────────────────

def _rejected(attempt_no: int = 1, base: str = "h" * 40,
              output_sha: str = "o" * 40, findings: int = 2,
              chain_length: int = 0,
              integration_head=None) -> st.AttemptRecord:
    extra = {rp.REVIEW_REJECTED_KEY: True,
             rp.REVIEW_FINDINGS_COUNT_KEY: findings,
             rp.REVIEW_OUTPUT_SHA_KEY: output_sha}
    if chain_length:
        extra[st.REPAIR_KEY] = {
            "attempt_no": attempt_no - 1,
            "integration_head": integration_head or base,
            "chain_length": chain_length,
        }
    return st.AttemptRecord(
        run_id="run1", node_id="a", attempt_no=attempt_no, base_sha=base,
        state=st.NodeState.PENDING, retry_class=st.RetryClass.SEMANTIC,
        extra=extra)


def _facts(prior, head: str = "h" * 40, **kw) -> rp.RepairFacts:
    stored = (prior.extra or {}).get(rp.REVIEW_OUTPUT_SHA_KEY) if prior else None
    base = dict(integration_head=head, prior=prior,
                rejected_ref_sha=stored, output_proven=stored is not None)
    base.update(kw)
    return rp.RepairFacts(**base)


class DecideRepairTests(unittest.TestCase):
    """Unit coverage of the refusals, each named by its own reason constant."""

    def test_no_prior_attempt_is_not_a_repair(self):
        decision = rp.decide_repair(_facts(None))
        self.assertIsNone(decision.basis)
        self.assertEqual(decision.reason, rp.REPAIR_NO_PRIOR_REJECTION)

    def test_the_marker_without_the_class_is_refused(self):
        prior = _rejected()
        prior.retry_class = st.RetryClass.ENVIRONMENTAL
        self.assertEqual(rp.decide_repair(_facts(prior)).reason,
                         rp.REPAIR_NO_PRIOR_REJECTION)

    def test_the_class_without_the_marker_is_refused(self):
        prior = _rejected()
        del prior.extra[rp.REVIEW_REJECTED_KEY]
        self.assertEqual(rp.decide_repair(_facts(prior)).reason,
                         rp.REPAIR_NO_PRIOR_REJECTION)

    def test_a_moved_ref_is_refused(self):
        prior = _rejected()
        self.assertEqual(
            rp.decide_repair(_facts(prior, rejected_ref_sha="z" * 40)).reason,
            rp.REPAIR_OUTPUT_UNPROVEN)

    def test_an_unprovable_commit_is_refused(self):
        prior = _rejected()
        self.assertEqual(
            rp.decide_repair(_facts(prior, output_proven=False)).reason,
            rp.REPAIR_OUTPUT_UNPROVEN)

    def test_a_row_with_no_stored_output_commit_is_refused(self):
        """Every row written before `REVIEW_OUTPUT_SHA_KEY` existed."""
        prior = _rejected()
        del prior.extra[rp.REVIEW_OUTPUT_SHA_KEY]
        self.assertEqual(rp.decide_repair(_facts(prior)).reason,
                         rp.REPAIR_OUTPUT_UNPROVEN)

    def test_a_moved_integration_head_is_refused(self):
        prior = _rejected()
        self.assertEqual(rp.decide_repair(_facts(prior, head="n" * 40)).reason,
                         rp.REPAIR_HEAD_MOVED)

    def test_the_chain_limit_is_the_bound(self):
        prior = _rejected(attempt_no=4, chain_length=rp.REPAIR_CHAIN_LIMIT)
        self.assertEqual(rp.decide_repair(_facts(prior)).reason,
                         rp.REPAIR_CHAIN_EXHAUSTED)
        below = _rejected(attempt_no=3, chain_length=rp.REPAIR_CHAIN_LIMIT - 1)
        self.assertEqual(rp.decide_repair(_facts(below)).reason,
                         rp.REPAIR_ADMITTED)

    def test_rising_findings_break_the_chain(self):
        prior = _rejected(attempt_no=2, findings=3, chain_length=1)
        decision = rp.decide_repair(_facts(prior, repaired_findings=1))
        self.assertIsNone(decision.basis)
        self.assertEqual(decision.reason, rp.REPAIR_FINDINGS_ROSE)

    def test_an_unknown_earlier_count_never_breaks_the_chain(self):
        """`None` is unknown, never zero: a row written before the count
        existed must not read as "the reviewer found nothing"."""
        prior = _rejected(attempt_no=2, findings=3, chain_length=1)
        self.assertEqual(
            rp.decide_repair(_facts(prior, repaired_findings=None)).reason,
            rp.REPAIR_ADMITTED)

    def test_an_admitted_basis_carries_every_fact_the_row_needs(self):
        prior = _rejected()
        basis = rp.decide_repair(_facts(prior)).basis
        self.assertEqual(basis.base_sha, "o" * 40)
        self.assertEqual(basis.integration_head, "h" * 40)
        self.assertEqual(basis.repair_of_attempt, 1)
        self.assertEqual(basis.chain_length, 1)
        self.assertEqual(rp.repair_extra(basis), {st.REPAIR_KEY: {
            "attempt_no": 1, "integration_head": "h" * 40, "chain_length": 1}})


class AttemptRowReadersTests(unittest.TestCase):
    """The three marker readers, and the ledger scope that depends on them."""

    def test_an_ordinary_row_reads_its_base_as_the_integration_head(self):
        row = st.AttemptRecord(run_id="run1", node_id="a", attempt_no=1,
                               base_sha="h" * 40)
        self.assertEqual(row.integration_head, "h" * 40)
        self.assertEqual(row.repair_chain_length, 0)
        self.assertIsNone(row.repair_of_attempt)
        self.assertEqual(row.guidance_key, ("a", "h" * 40))

    def test_a_repair_row_reads_the_head_from_its_marker(self):
        row = st.AttemptRecord(
            run_id="run1", node_id="a", attempt_no=2, base_sha="o" * 40,
            extra={st.REPAIR_KEY: {"attempt_no": 1,
                                   "integration_head": "h" * 40,
                                   "chain_length": 1}})
        self.assertEqual(row.integration_head, "h" * 40)
        self.assertEqual(row.repair_chain_length, 1)
        self.assertEqual(row.repair_of_attempt, 1)
        # The scope the guidance ledger is keyed by, which is what carries the
        # findings across a repair chain instead of emptying at every attempt.
        self.assertEqual(row.guidance_key, ("a", "h" * 40))

    def test_a_malformed_marker_falls_back_to_the_base(self):
        row = st.AttemptRecord(run_id="run1", node_id="a", attempt_no=2,
                               base_sha="o" * 40,
                               extra={st.REPAIR_KEY: "not a mapping"})
        self.assertEqual(row.integration_head, "o" * 40)
        self.assertEqual(row.repair_chain_length, 0)
        self.assertIsNone(row.repair_of_attempt)

    def test_repaired_findings_count_follows_the_marker(self):
        first = _rejected(attempt_no=1, findings=2)
        second = _rejected(attempt_no=2, findings=1, chain_length=1)
        self.assertEqual(
            rp.repaired_findings_count([first, second], second), 2)
        self.assertIsNone(rp.repaired_findings_count([first, second], first))
        self.assertIsNone(rp.repaired_findings_count([second], second))


if __name__ == "__main__":
    unittest.main()
