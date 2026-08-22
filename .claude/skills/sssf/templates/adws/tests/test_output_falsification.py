"""§7.4's other half: the gate must still be falsifiable *after* the work.

The defect this closes, from the lifecycle store rather than from reasoning.
Maestro's node contract has each lane write both its production code and the
test file its own gate counts. For `lane-p5-gap-policy` of
`cmo-consolidation-l-r7` the gate is `pytest
tests/unit/ingestion/test_cmo_gap_policy.py` with `min_cases: 9`, and that
test file is one of the node's two declared outputs. The thing being satisfied
is written by the thing satisfying it, and `min_cases` counts cases — it
cannot tell nine real assertions from nine hollow ones.

Every mechanism stacked on top of that was a model asked to grade an exam the
student wrote, and §19 M35 removed the last of them. What replaces it is one
counted, model-free question, asked once per agent attempt after its post-node
gate has gone green:

    revert everything this node wrote that its own gate's argv does not
    select, and run the gate again. It MUST fail.

For p5 that means reverting `gap_policy.py`, leaving `test_cmo_gap_policy.py`,
and requiring the gate to go red. Nine tests that never touch the production
file survive its deletion and are refused. It reads `node.outputs` and
`node.gate` and nothing else — both already present in the shipped
`maestro-plan.v2` — so it works on a shipped plan with no re-ship.

What each layer of this file pins:

* the selector predicate, which decides what "the gate does not name this"
  means, including the direction that is not obvious — an argv token beneath a
  broadly declared output must not cause that output to be reverted out from
  under the gate reading it;
* the adjudicator, which is `adjudicate_gate`'s own §10.2 counting rule
  re-asked, never a second definition of green;
* the git mechanics, over a **real repository and a real attempt worktree** —
  revert to a base, restore from the sealed output commit, and the new-file
  case where "revert" means absence;
* and the whole loop through the real `Scheduler`, with a gate that reads the
  worktree it is given instead of being scripted. That last property is the
  one every other scheduler test in this suite lacks and the one this check is
  about: a gate that answers the same way whatever the tree holds cannot
  witness anything, and a fixture that scripts the gate cannot tell a hollow
  test file from an honest one either.

Run with:  uv run adws/adw_test.py -k output_falsification
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from adw_modules import retry_policy as rp  # noqa: E402
from adw_modules import scheduler as sch  # noqa: E402
from adw_modules import scheduler_types as st  # noqa: E402
from adw_modules import verification as vf  # noqa: E402
from adw_modules import worktree as wt  # noqa: E402

from test_scheduler import SchedulerFixture, _git, green, red  # noqa: E402

#: The r7 lane this was written from, verbatim from
#: `.maestro/plans/cmo-consolidation-l-r7/maestro-plan.v1`, whose
#: `schema_version` is `maestro-plan.v2` and whose digest is `e509c149…`.
P5_OUTPUTS = ("src/lexgenius_pipeline/ingestion/judicial/cmo/gap_policy.py",
              "tests/unit/ingestion/test_cmo_gap_policy.py")
P5_ARGV = ("tests/unit/ingestion/test_cmo_gap_policy.py",)


# ── the selector predicate ──────────────────────────────────────────────────

class OutputsUnnamedByGateTests(unittest.TestCase):

    def test_the_r7_lane_reverts_its_production_file_and_keeps_its_tests(self):
        """The case this exists for, on the real plan's real strings."""
        self.assertEqual((P5_OUTPUTS[0],),
                         vf.outputs_unnamed_by_gate(P5_OUTPUTS, P5_ARGV))

    def test_a_node_id_suffix_still_selects_its_file(self):
        """`tests/t.py::TestX::test_y` selects `tests/t.py`. Read literally it
        matches no path at all, and the file would be reverted out from under
        the gate that names it."""
        self.assertEqual(
            (), vf.outputs_unnamed_by_gate(
                ("tests/t.py",), ("tests/t.py::TestX::test_y",)))

    def test_a_directory_token_selects_the_files_beneath_it(self):
        self.assertEqual(
            ("src/a.py",),
            vf.outputs_unnamed_by_gate(("src/a.py", "tests/unit/test_a.py"),
                                       ("tests/unit",)))

    def test_a_directory_output_is_selected_by_a_token_beneath_it(self):
        """The direction that is not obvious. A node declaring `tests/` as an
        output, gated on one file inside it, must not have `tests/` reverted —
        that would take the gate's own selector with it and the gate would go
        red for a reason that says nothing about the code."""
        self.assertEqual(
            (), vf.outputs_unnamed_by_gate(
                ("tests",), ("tests/unit/test_a.py",)))

    def test_flags_are_not_paths(self):
        """`-q`, `-x`, `--tb=short` name nothing, and a token beginning with a
        dash must never be read as selecting a path."""
        self.assertEqual(
            ("src/a.py",),
            vf.outputs_unnamed_by_gate(("src/a.py",), ("-q", "--tb=short")))

    def test_an_empty_argv_names_nothing(self):
        self.assertEqual(("src/a.py",),
                         vf.outputs_unnamed_by_gate(("src/a.py",), ()))


# ── the adjudicator ─────────────────────────────────────────────────────────

class AdjudicateOutputFalsificationTests(unittest.TestCase):

    def test_a_red_gate_after_the_revert_is_the_witness(self):
        verdict = vf.adjudicate_output_falsification(
            vf.adjudicate_gate(red(), 1), ("src/a.py",))
        self.assertTrue(verdict.verified)

    def test_a_gate_that_survives_the_revert_is_refused(self):
        verdict = vf.adjudicate_output_falsification(
            vf.adjudicate_gate(green(9), 9), ("src/a.py",))
        self.assertFalse(verdict.verified)
        self.assertIs(verdict.retry_class, st.RetryClass.SEMANTIC)
        self.assertEqual(verdict.offending_paths, ("src/a.py",))
        self.assertIn("src/a.py", verdict.reason)

    def test_the_counting_rule_is_the_same_one_that_admitted_the_post_gate(self):
        """Not a second definition of green. A gate that exits zero with eight
        passing cases against a threshold of nine has not satisfied §10.2, and
        must count as falsified here for exactly the reason it would have
        failed clause 3 there."""
        self.assertTrue(vf.adjudicate_output_falsification(
            vf.adjudicate_gate(green(8), 9), ("src/a.py",)).verified)
        self.assertFalse(vf.adjudicate_output_falsification(
            vf.adjudicate_gate(green(9), 9), ("src/a.py",)).verified)

    def test_nothing_to_revert_is_not_verified(self):
        """Empty `reverted` is a counted no-subject, not a pass (#123).

        The old claim was `verified=True` for a check that never ran. Honest
        nodes have a production path the gate does not select; that path is
        the green control in `TheLoopTests`. Tests nodes never reach this
        adjudicator.
        """
        verdict = vf.adjudicate_output_falsification(
            vf.adjudicate_gate(green(9), 9), ())
        self.assertFalse(verdict.verified)
        self.assertIs(verdict.retry_class, st.RetryClass.SEMANTIC)
        self.assertIn("FALSIFICATION_NO_SUBJECT", verdict.reason)


# ── the git mechanics, over a real worktree ─────────────────────────────────

class RevertAndRestoreTests(SchedulerFixture):
    """`paths_written_since`, `revert_paths_to` and `restore_paths_from_head`
    against a real attempt worktree with a real sealed output commit."""

    def _attempt_with_work(self, files):
        """One attempt worktree carrying `files`, committed exactly as the
        scheduler commits: measured delta, private index, sealed output SHA."""
        base = wt.integration_head(self.repo, "integration/run1")
        attempt = wt.create_attempt_worktree(
            self.repo, "run1", "a", 1, integration_head=base,
            worktrees_root=self.root / "wt",
            scratch_root=self.root / "scratch")
        baseline = wt.take_baseline(attempt)
        for rel, content in files.items():
            target = attempt.path / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        after = wt.inventory(attempt.path)
        measured = wt.delta(baseline, after)
        output_sha = wt.commit_measured_delta(
            attempt, measured, after, "a attempt 1")
        return attempt, base, output_sha

    def test_it_lists_what_the_node_wrote_and_not_the_base(self):
        attempt, base, _sha = self._attempt_with_work(
            {"src/a.py": "A\n", "tests/test_a.py": "T\n"})
        self.assertEqual(
            ("src/a.py", "tests/test_a.py"),
            tuple(sorted(wt.paths_written_since(attempt, base))))

    def test_a_new_file_is_reverted_by_being_absent(self):
        attempt, base, _sha = self._attempt_with_work(
            {"src/a.py": "A\n", "tests/test_a.py": "T\n"})
        wt.revert_paths_to(attempt, base, ("src/a.py",))
        self.assertFalse((attempt.path / "src/a.py").exists())
        # And only that one: the gate's own subject is untouched.
        self.assertTrue((attempt.path / "tests/test_a.py").exists())

    def test_a_modified_file_is_reverted_to_its_base_content(self):
        base = wt.integration_head(self.repo, "integration/run1")
        (self.integration / "src").mkdir()
        (self.integration / "src" / "a.py").write_text("ORIGINAL\n")
        _git(self.integration, "add", "-A")
        _git(self.integration, "commit", "-q", "-m", "seed")
        attempt, base, _sha = self._attempt_with_work({"src/a.py": "CHANGED\n"})

        wt.revert_paths_to(attempt, base, ("src/a.py",))
        self.assertEqual("ORIGINAL\n", (attempt.path / "src/a.py").read_text())

    def test_the_restore_returns_the_tree_to_the_sealed_commit(self):
        attempt, base, output_sha = self._attempt_with_work(
            {"src/a.py": "A\n", "tests/test_a.py": "T\n"})
        before = wt.inventory(attempt.path)

        reverted = wt.revert_paths_to(attempt, base, ("src/a.py",))
        wt.restore_paths_from_head(attempt, reverted)

        self.assertEqual("A\n", (attempt.path / "src/a.py").read_text())
        self.assertEqual(before, wt.inventory(attempt.path))
        # The commit was never at risk — it is what the merge consumes — and
        # the ref still holds it.
        self.assertEqual(
            output_sha,
            wt.attempt_ref_commit(self.repo, "run1", "a", 1))


# ── the loop, through the real scheduler, with a gate that reads the tree ───

class GateReadsTheTreeFixture(SchedulerFixture):
    """A node shaped like r7's `lane-p5-gap-policy`, and a gate that answers
    from the worktree rather than from a script.

    The gate here is one line of real logic — the test file passes only if the
    production module it is supposed to exercise is present — which is the
    smallest honest stand-in for `pytest tests/…` collecting an import that
    resolves. Everything else on the path is production: the scheduler's
    ordering, `worktree`'s git, and `verification`'s adjudication.
    """

    PROD = "src/gap_policy.py"
    TEST = "tests/test_gap_policy.py"

    def agent(self, node_id="p5", depth=0, needs=(), outputs=None,
              specs=()):
        return st.PlanNode(
            node_id=node_id, kind=st.NodeKind.AGENT, depth=0,
            outputs=(self.PROD, self.TEST),
            instruction="Build the gap policy and the tests that witness it.",
            gate_command=("pytest", self.TEST),
            gate_selector=self.TEST, gate_min_cases=2)

    PROD_BODY = ("def classify(gap):\n"
                 "    return 'wide' if gap > 3 else 'narrow'\n")

    #: The honest pair: the test imports the module and calls into it, so
    #: deleting the module takes the two passing cases with it.
    HONEST_TEST = ("import gap_policy\n"
                   "assert gap_policy.classify(9) == 'wide'\n"
                   "assert gap_policy.classify(1) == 'narrow'\n")

    #: The hollow pair, and it is hollow in the one way that gets past every
    #: check that already exists. It **references** `classify`, so #118's
    #: unreferenced-produced-symbol refusal is satisfied; it collects two
    #: cases, so `min_cases` is satisfied; it is inside the node's declared
    #: outputs, so §8.3 is satisfied. What it never does is execute
    #: `gap_policy.py` — it carries its own copy of the function — so it goes
    #: on passing when the production file is taken back out. This is the
    #: shape `lane-p5-gap-policy` could have shipped 39 times.
    HOLLOW_TEST = ("def classify(gap):\n"
                   "    return 'wide' if gap > 3 else 'narrow'\n"
                   "assert classify(9) == 'wide'\n"
                   "assert classify(1) == 'narrow'\n")

    def builder(self, hollow: bool):
        def run_node(attempt, node, record, retry_prompt, on_launch,
                     cancel_requested):
            self.prompts.setdefault(node.node_id, []).append(retry_prompt)
            on_launch(None)
            for rel in (self.PROD, self.TEST):
                (attempt.path / rel).parent.mkdir(parents=True, exist_ok=True)
            (attempt.path / self.PROD).write_text(self.PROD_BODY)
            (attempt.path / self.TEST).write_text(
                self.HOLLOW_TEST if hollow else self.HONEST_TEST)
            return sch.NodeExecution(envelope_parsed=True, exit_code=0)
        return run_node

    def tree_reading_gate(self):
        """A gate that answers from the tree, the way pytest would.

        Red when there is no test file to collect. Red when the test file
        imports a module that is not there — which is what an interpreter does
        to an honest test whose subject has been reverted. Green otherwise,
        including for a test that carries its own copy of the code and
        therefore never needed the module at all.
        """
        self.gate_calls = []

        def run_gate(attempt, node, phase, cancel_requested):
            self.gate_calls.append(phase)
            test = attempt.path / self.TEST
            if not test.exists():
                return red(passed=0)
            body = test.read_text()
            imports_prod = "import gap_policy" in body
            if imports_prod and not (attempt.path / self.PROD).exists():
                return red(passed=0)
            return green(2)

        return run_gate


class TheLoopTests(GateReadsTheTreeFixture):

    def test_an_honest_node_merges_and_the_gate_ran_three_times(self):
        node = self.agent()
        report = self.schedule(
            [node],
            deps=self.deps(run_node=self.builder(hollow=False),
                           run_gate=self.tree_reading_gate())).run()

        self.assertIs(report.outcome, st.RunOutcome.ACCEPTED)
        self.assertEqual(self.states()["p5"], st.NodeState.MERGED.value)
        # pre (red: nothing written yet), post (green), falsify (red: the
        # production module was taken back out). The stated cost is exactly
        # one extra gate run per attempt.
        self.assertEqual(["pre", "post", "falsify"], self.gate_calls)

    def test_a_hollow_test_file_is_refused_however_green_its_gate_was(self):
        """B1 and B2, convicted by a count. The post-node gate is green, the
        permission check passes, the commit is sealed — and the same gate is
        still green with the production file reverted, which says the nine
        cases never observed it."""
        node = self.agent()
        report = self.schedule(
            [node],
            config=self.config(semantic_ceiling=1),
            deps=self.deps(run_node=self.builder(hollow=True),
                           run_gate=self.tree_reading_gate())).run()

        self.assertIs(report.outcome, st.RunOutcome.BLOCKED)
        node_row = self.store.get_node("run1", "p5")
        self.assertIs(node_row.state, st.NodeState.BLOCKED)
        self.assertIs(node_row.block_reason,
                      st.BlockReason.SEMANTIC_BUDGET_EXHAUSTED)

    def test_the_refusal_names_the_paths_and_is_semantic(self):
        """§7.5: an agent is not deterministic and a prompt naming the paths
        its tests must exercise is genuinely new instructions, so this earns a
        retry rather than blocking terminally."""
        self.schedule(
            [self.agent()],
            config=self.config(semantic_ceiling=2),
            deps=self.deps(run_node=self.builder(hollow=True),
                           run_gate=self.tree_reading_gate())).run()

        rows = sorted(self.store.attempts_for("run1", "p5"),
                      key=lambda row: row.attempt_no)
        self.assertIs(rows[0].retry_class, st.RetryClass.SEMANTIC)
        detail = [row.get("detail") or {}
                  for row in self.store.audit_transitions("run1", "p5")
                  if row.get("reason") == "retry:SEMANTIC"]
        self.assertEqual([self.PROD], detail[0]["offending_paths"])

    def test_the_refused_attempt_is_repaired_rather_than_re_implemented(self):
        """The refusal arrives with a sealed, provable output commit, so
        `decide_repair` bases the next attempt on it (§7.5). The whole reason
        the repair chain outlived the review rejection it was built for."""
        self.schedule(
            [self.agent()],
            config=self.config(semantic_ceiling=2),
            deps=self.deps(run_node=self.builder(hollow=True),
                           run_gate=self.tree_reading_gate())).run()

        rows = sorted(self.store.attempts_for("run1", "p5"),
                      key=lambda row: row.attempt_no)
        self.assertEqual(2, len(rows))
        refused_sha = rows[0].extra[rp.REVIEW_OUTPUT_SHA_KEY]
        self.assertEqual(rows[1].base_sha, refused_sha)
        self.assertEqual(rows[1].repair_of_attempt, 1)

    def test_the_worktree_is_intact_after_the_check(self):
        """The revert is undone. §8.4 already makes the commit safe — the
        merge consumes the sealed object — but a tree left mid-revert would
        make §8.3's pre-merge report name paths nobody touched."""
        self.schedule(
            [self.agent()],
            deps=self.deps(run_node=self.builder(hollow=False),
                           run_gate=self.tree_reading_gate())).run()

        merged = wt.integration_head(self.repo, "integration/run1")
        listed = _git(self.integration, "ls-tree", "-r", "--name-only", merged)
        self.assertIn(self.PROD, listed.splitlines())
        self.assertIn(self.TEST, listed.splitlines())


class TheCheckHasNoSubjectTests(GateReadsTheTreeFixture):
    """A node that wrote nothing outside its own gate's selector.

    The hollow shape in its purest form. The count is `len(unnamed) == 0`.
    It must not merge while claiming it was verified (#123).
    """

    def builder(self, hollow=True):
        def run_node(attempt, node, record, retry_prompt, on_launch,
                     cancel_requested):
            self.prompts.setdefault(node.node_id, []).append(retry_prompt)
            on_launch(None)
            (attempt.path / self.TEST).parent.mkdir(parents=True, exist_ok=True)
            (attempt.path / self.TEST).write_text("assert True\n")
            return sch.NodeExecution(envelope_parsed=True, exit_code=0)
        return run_node

    def test_it_does_not_merge_claiming_it_was_verified(self):
        self.schedule(
            [self.agent()],
            config=self.config(semantic_ceiling=1),
            deps=self.deps(run_node=self.builder(),
                           run_gate=self.tree_reading_gate())).run()

        self.assertNotEqual(self.states()["p5"], st.NodeState.MERGED.value)
        self.assertIs(self.store.get_node("run1", "p5").state,
                      st.NodeState.BLOCKED)
        rows = sorted(self.store.attempts_for("run1", "p5"),
                      key=lambda row: row.attempt_no)
        self.assertIs(rows[0].retry_class, st.RetryClass.SEMANTIC)
        audits = self.store.audit_transitions("run1", "p5")
        self.assertTrue(
            any("FALSIFICATION_NO_SUBJECT" in str(row.get("detail"))
                for row in audits),
            audits)
        # Nothing to take out, so the third gate run is still not paid for.
        self.assertEqual(["pre", "post"], self.gate_calls)


if __name__ == "__main__":
    unittest.main()
