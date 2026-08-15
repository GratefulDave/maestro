"""Executable proof of the VERIFIED predicate and result adjudication.

`VERIFIED` is a named predicate, not a vibe, and it is defined **per node
kind** (§7.3). Its parts already existed separately across §7.4, §8.3, and
§10.2; what was missing was a single name, which is why "the gates were
green" could be mistaken for "the work was verified."

The tests are grouped by the section they settle:

  §10.2  one shared counting rule — a green exit code is not a passing gate
  §7.3   the agent-node conjunction, its evaluation order, and its bound
  §7.3   the code-node predicate, which the four clauses would wedge
  §10.1  no guard reads free text
  §7.7   results adjudicate against the attempt they name, payload retained

Run with:  uv run adws/adw_test.py -k verification
"""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

from adw_modules import scheduler_types as st  # noqa: E402
from adw_modules import verification as vf  # noqa: E402
from adw_modules import worktree as wt  # noqa: E402


def counts(collected=None, passed=0, failed=0, skipped=0, errored=0):
    raw = {"passed": passed, "failed": failed, "skipped": skipped,
           "error": errored}
    if collected is not None:
        raw["collected"] = collected
    return raw


# ── §10.2 the counting rule ─────────────────────────────────────────────────

class CountingRuleTests(unittest.TestCase):

    def test_a_gate_passes_under_the_full_conjunction(self):
        verdict = vf.adjudicate_counts(counts(collected=3, passed=3), min_cases=1)
        self.assertTrue(verdict.green)

    def test_min_cases_below_one_is_refused(self):
        """`passed >= min_cases >= 1` — a gate demanding zero cases is a gate
        that cannot fail, which is the thing this rule exists to catch."""
        with self.assertRaises(ValueError):
            vf.adjudicate_counts(counts(collected=3, passed=3), min_cases=0)

    def test_fewer_passes_than_min_cases_fails(self):
        verdict = vf.adjudicate_counts(counts(collected=3, passed=2), min_cases=3)
        self.assertFalse(verdict.green)

    def test_all_skipped_fails_even_with_a_zero_exit(self):
        """`skipped < collected`. A suite that skipped everything exits zero
        and has asserted nothing."""
        verdict = vf.adjudicate_counts(counts(collected=3, passed=0, skipped=3),
                                       min_cases=1)
        self.assertFalse(verdict.green)

    def test_a_failing_case_fails_the_gate(self):
        """§10.2 states the conjunction as `passed >= min_cases >= 1 AND
        skipped < collected AND errored == 0` — `failed` is in the parser's
        five-tuple and in no clause. Taken literally, a node whose own scoped
        gate reports one pass and one failure verifies at min_cases 1 with
        its declared behaviour demonstrably absent, which contradicts §7.3
        clause 3 and §7.4's falsifiability argument outright. The rule is
        implemented with `failed == 0` and the omission is a §10.2 defect."""
        verdict = vf.adjudicate_counts(counts(collected=2, passed=1, failed=1),
                                       min_cases=1)
        self.assertFalse(verdict.green)
        self.assertIn("failed", verdict.reason)

    def test_one_error_fails_however_many_passed(self):
        verdict = vf.adjudicate_counts(counts(collected=9, passed=8, errored=1),
                                       min_cases=1)
        self.assertFalse(verdict.green)

    def test_a_placeholder_that_ran_nothing_is_caught_structurally(self):
        """§10.2 — the echo command the base ships as its default quality
        layer collects zero cases and exits zero. No lexical blocklist: a
        blocklist would violate the same rule that forbids classifying on
        process output."""
        verdict = vf.adjudicate_counts({}, min_cases=1)
        self.assertFalse(verdict.green)
        self.assertTrue(verdict.unparseable)

    def test_unparseable_is_fail_closed_and_environmental(self):
        """§10.2 — not a verdict about the code under test."""
        verdict = vf.adjudicate_counts({}, min_cases=1)
        self.assertIs(verdict.retry_class, st.RetryClass.ENVIRONMENTAL)

    def test_a_parsed_report_is_never_environmental(self):
        """A real red suite is content, not infrastructure."""
        verdict = vf.adjudicate_counts(counts(collected=2, passed=1, failed=1),
                                       min_cases=1)
        self.assertFalse(verdict.green)
        self.assertFalse(verdict.unparseable)
        self.assertIsNone(verdict.retry_class)

    def test_collected_is_derived_when_the_report_omits_it(self):
        """The summary line partitions the collected set, so the four outcome
        counts sum to it. Deriving is honest; refusing would fail every
        runner whose summary names outcomes but not the total."""
        verdict = vf.adjudicate_counts(counts(passed=2, skipped=1), min_cases=1)
        self.assertTrue(verdict.green)
        self.assertEqual(verdict.counts.collected, 3)

    def test_an_explicit_collected_wins_over_the_sum(self):
        """A runner that collected 10 and reported 3 outcomes lost 7, and the
        rule must see the loss rather than smooth it away."""
        verdict = vf.adjudicate_counts(counts(collected=10, passed=3), min_cases=1)
        self.assertEqual(verdict.counts.collected, 10)

    def test_it_consumes_the_merge_protocol_gate_result(self):
        """§10.2 — *one* shared parser, consumed by node gates and by
        integration acceptance alike, never two."""
        result = wt.GateResult(label="node-gate", scope="node", selector="tests/",
                               command=("pytest",), exit_code=0, green=True,
                               counts={"passed": 4})
        verdict = vf.adjudicate_gate(result, min_cases=1)
        self.assertTrue(verdict.green)

    def test_a_zero_exit_does_not_make_a_gate_green(self):
        """The base's test gate was a pure exit-code check and could not
        detect a suite that ran zero tests. This is that repair, executed."""
        result = wt.GateResult(label="node-gate", scope="node", selector="tests/",
                               command=("echo", "ok"), exit_code=0, green=True,
                               counts={})
        self.assertFalse(vf.adjudicate_gate(result, min_cases=1).green)

    def test_a_nonzero_exit_is_red_even_with_clean_counts(self):
        result = wt.GateResult(label="node-gate", scope="node", selector="tests/",
                               command=("pytest",), exit_code=1, green=False,
                               counts={"passed": 12})
        verdict = vf.adjudicate_gate(result, min_cases=1)
        self.assertFalse(verdict.green)
        self.assertFalse(verdict.unparseable)
        self.assertEqual(verdict.counts.passed, 12)


# ── §7.3 the agent-node conjunction ─────────────────────────────────────────

class AgentNodeVerificationTests(unittest.TestCase):

    def ok(self, **kw):
        base = dict(envelope_parsed=True,
                    pre_gate=vf.adjudicate_counts(counts(collected=2, passed=1,
                                                         failed=1), min_cases=1),
                    post_gate=vf.adjudicate_counts(counts(collected=2, passed=2),
                                                   min_cases=1),
                    permission=wt.PermissionVerdict(passes=True))
        base.update(kw)
        return vf.verify_agent_node(**base)

    def test_all_four_clauses_hold(self):
        verdict = self.ok()
        self.assertTrue(verdict.verified)
        self.assertIsNone(verdict.block_reason)

    def test_an_unparsed_envelope_fails_clause_one(self):
        verdict = self.ok(envelope_parsed=False)
        self.assertFalse(verdict.verified)
        self.assertEqual(verdict.failed_clause, 1)

    def test_a_green_pre_gate_is_not_falsifiable(self):
        """§7.4 — clause 2. Blocked, terminal, non-retryable: re-running an
        agent cannot make a gate falsifiable."""
        verdict = self.ok(pre_gate=vf.adjudicate_counts(counts(collected=2, passed=2),
                                                        min_cases=1))
        self.assertFalse(verdict.verified)
        self.assertEqual(verdict.failed_clause, 2)
        self.assertIs(verdict.block_reason, st.BlockReason.GATE_NOT_FALSIFIABLE)
        self.assertFalse(st.is_retryable(verdict.block_reason))

    def test_a_red_post_gate_fails_clause_three(self):
        verdict = self.ok(post_gate=vf.adjudicate_counts(
            counts(collected=2, passed=1, failed=1), min_cases=1))
        self.assertFalse(verdict.verified)
        self.assertEqual(verdict.failed_clause, 3)
        self.assertIsNone(verdict.block_reason)

    def test_a_permission_failure_is_semantic_for_an_agent(self):
        """§7.5 — deliberately not in the non-retryable family. An agent is
        not deterministic, so a retry prompt naming the offending paths is
        genuinely new instructions."""
        verdict = self.ok(permission=wt.PermissionVerdict(
            passes=False, conjunct1_violations=("src/elsewhere.py",)))
        self.assertFalse(verdict.verified)
        self.assertEqual(verdict.failed_clause, 4)
        self.assertIs(verdict.retry_class, st.RetryClass.SEMANTIC)
        self.assertIsNone(verdict.block_reason)
        self.assertIn("src/elsewhere.py", verdict.offending_paths)

    def test_clause_four_is_evaluated_before_clause_three(self):
        """§7.3 — the clauses are not evaluated in list order. Clause 4 is
        evaluated at measurement and the commit follows it immediately;
        clause 3 is evaluated afterwards, against the committed tree. A node
        failing both must report the permission failure, because that is the
        one that stopped the attempt before a commit existed."""
        verdict = self.ok(
            permission=wt.PermissionVerdict(passes=False, conjunct1_violations=("x.py",)),
            post_gate=vf.adjudicate_counts(counts(collected=1, failed=1),
                                           min_cases=1))
        self.assertEqual(verdict.failed_clause, 4)

    def test_clause_two_is_evaluated_before_clause_four(self):
        """The pre-gate runs before the agent does, so a green pre-gate stops
        the attempt before any delta exists to check."""
        verdict = self.ok(
            pre_gate=vf.adjudicate_counts(counts(collected=1, passed=1), min_cases=1),
            permission=wt.PermissionVerdict(passes=False, conjunct1_violations=("x.py",)))
        self.assertEqual(verdict.failed_clause, 2)

    def test_an_unparseable_post_gate_is_environmental_not_a_verdict(self):
        verdict = self.ok(post_gate=vf.adjudicate_counts({}, min_cases=1))
        self.assertFalse(verdict.verified)
        self.assertIs(verdict.retry_class, st.RetryClass.ENVIRONMENTAL)

    def test_the_predicate_states_its_own_bound(self):
        """§7.3 — VERIFIED asserts this node's declared behaviour at this
        base and that its writes were authorized. It does not assert the
        repository still passes as a whole; that claim is made once, by the
        integration gate at the final head."""
        self.assertIn("integration gate", vf.verify_agent_node.__doc__)
        self.assertFalse(self.ok().asserts_repository_wide)


# ── §7.3 the code-node predicate ────────────────────────────────────────────

class CodeNodeVerificationTests(unittest.TestCase):

    def ok(self, **kw):
        base = dict(exit_code=0, permission=wt.PermissionVerdict(passes=True),
                    diff_empty=True, expects_changes=False)
        base.update(kw)
        return vf.verify_code_node(**base)

    def test_exit_zero_with_an_empty_diff_verifies_by_default(self):
        """§6.7's assertive "nothing broke" node is the common case, and an
        empty diff is its normal result. An earlier draft required a
        non-empty diff whenever the node declared outputs, which convicted
        exactly the node §6.7 prescribes."""
        self.assertTrue(self.ok().verified)

    def test_a_nonzero_exit_fails(self):
        verdict = self.ok(exit_code=1)
        self.assertFalse(verdict.verified)

    def test_the_four_agent_clauses_do_not_apply(self):
        """Unscoped, clause 2 and clause 3 are unsatisfiable for a code node,
        which has no gate and no min_cases — so it could never be VERIFIED,
        never merge, and would wedge its subtree, breaking §6.7's own
        recommended composition."""
        self.assertIsNone(self.ok().failed_clause)

    def test_expects_changes_true_with_an_empty_diff_blocks(self):
        """§7.3 — `CODE_NODE_NO_EFFECT`. The discrimination has to be
        authored, not inferred: diff-emptiness cannot itself tell a broken
        codemod from an idempotent one."""
        verdict = self.ok(expects_changes=True, diff_empty=True)
        self.assertFalse(verdict.verified)
        self.assertIs(verdict.block_reason, st.BlockReason.CODE_NODE_NO_EFFECT)
        self.assertFalse(st.is_retryable(verdict.block_reason))

    def test_expects_changes_true_with_a_real_diff_verifies(self):
        self.assertTrue(self.ok(expects_changes=True, diff_empty=False).verified)

    def test_a_permission_failure_is_non_retryable_for_a_code_node(self):
        """§7.5 — the asymmetry against the agent case. A deterministic
        command that wrote outside its declaration is a plan defect, and
        re-running it cannot write different paths."""
        verdict = self.ok(permission=wt.PermissionVerdict(
            passes=False, conjunct1_violations=("build/out.o",)))
        self.assertFalse(verdict.verified)
        self.assertIs(verdict.block_reason,
                      st.BlockReason.PERMISSION_SCOPE_VIOLATION)
        self.assertFalse(st.is_retryable(verdict.block_reason))
        self.assertIsNot(verdict.retry_class, st.RetryClass.SEMANTIC)

    def test_no_effect_is_not_left_to_the_environmental_default(self):
        """Without a dedicated reason `_classify` sees nothing wrong, the
        failure falls to ENVIRONMENTAL, is retried twice with backoff,
        deterministically reproduces the same empty diff, and then blocks
        with an infra-flavoured reason for what is a fact about content."""
        verdict = self.ok(expects_changes=True)
        self.assertIsNot(verdict.retry_class, st.RetryClass.ENVIRONMENTAL)


# ── §10.1 no guard reads free text ──────────────────────────────────────────

FREE_TEXT_FIELDS = ("notes_for_next_agent", "summary")


class FreeTextDetectorTests(unittest.TestCase):
    """Prose is permitted as input to work; never as a guarantee about work."""

    def test_verification_reads_no_free_text_field(self):
        source = (ADWS / "adw_modules" / "verification.py").read_text()
        self.assertEqual(vf.free_text_reads(source), ())

    def test_the_detector_catches_a_planted_violation(self):
        """A detector never proven red on a real violation is not a
        detector. Both access shapes are planted, because catching only the
        attribute form would leave the dict form silently permitted."""
        planted = (
            "def guard(envelope):\n"
            "    if envelope.notes_for_next_agent:\n"
            "        return True\n"
            "    return envelope['summary'] == 'ok'\n")
        found = vf.free_text_reads(planted)
        self.assertIn("notes_for_next_agent", found)
        self.assertIn("summary", found)

    def test_the_detector_parses_rather_than_greps(self):
        """A field name inside a string or a comment is not a read, and a
        detector that convicted one would be the lexical matching this
        design forbids elsewhere."""
        benign = ('"""No guard may read notes_for_next_agent."""\n'
                  "MESSAGE = 'summary'\n")
        self.assertEqual(vf.free_text_reads(benign), ())
        ast.parse(benign)  # the fixture is real code, not a string blob


# ── §7.7 results and late arrivals ──────────────────────────────────────────

class AdjudicationTests(unittest.TestCase):

    def attempt(self, no=1, sha="a" * 40, state=st.NodeState.RUNNING):
        return st.AttemptRecord(run_id="r1", node_id="n1", attempt_no=no,
                                base_sha=sha, state=state)

    def result(self, no=1, sha="a" * 40):
        return st.ResultRecord(node_id="n1", attempt_no=no, subject_sha=sha,
                               payload={"verdict": "fail", "findings": 2})

    def test_a_matching_live_attempt_accepts(self):
        adjudged = vf.adjudicate_result(self.result(), [self.attempt()])
        self.assertIs(adjudged.adjudication, st.Adjudication.ACCEPTED)

    def test_a_superseded_attempt_is_named_as_such(self):
        """A reclaimed attempt's late result lands here — the direct repair
        of a correct FAIL carrying two real findings that vanished."""
        adjudged = vf.adjudicate_result(
            self.result(no=1), [self.attempt(no=1, state=st.NodeState.PENDING),
                                self.attempt(no=2)])
        self.assertIs(adjudged.adjudication, st.Adjudication.SUPERSEDED)

    def test_an_unknown_attempt_is_named_as_such(self):
        adjudged = vf.adjudicate_result(self.result(no=7), [self.attempt(no=1)])
        self.assertIs(adjudged.adjudication, st.Adjudication.UNKNOWN_ATTEMPT)

    def test_a_sha_mismatch_is_named_as_such(self):
        adjudged = vf.adjudicate_result(self.result(sha="b" * 40),
                                        [self.attempt(sha="a" * 40)])
        self.assertIs(adjudged.adjudication, st.Adjudication.SHA_MISMATCH)

    def test_the_payload_is_retained_in_all_four_outcomes(self):
        """§7.7 — the payload and the adjudication are the same row, so an
        adjudication cannot be recorded without its payload."""
        cases = [
            (self.result(), [self.attempt()]),
            (self.result(no=1), [self.attempt(no=1, state=st.NodeState.PENDING),
                                 self.attempt(no=2)]),
            (self.result(no=7), [self.attempt(no=1)]),
            (self.result(sha="b" * 40), [self.attempt(sha="a" * 40)]),
        ]
        seen = set()
        for result, attempts in cases:
            adjudged = vf.adjudicate_result(result, attempts)
            seen.add(adjudged.adjudication)
            self.assertEqual(adjudged.payload, result.payload)
        self.assertEqual(seen, set(st.Adjudication))

    def test_it_is_adjudicated_against_the_attempt_row_it_names(self):
        """§7.7 — never against the node's current state. A node already
        MERGED does not turn a matching live attempt's result into a
        supersession."""
        adjudged = vf.adjudicate_result(self.result(), [self.attempt()],
                                        node_state=st.NodeState.MERGED)
        self.assertIs(adjudged.adjudication, st.Adjudication.ACCEPTED)


if __name__ == "__main__":
    unittest.main()
