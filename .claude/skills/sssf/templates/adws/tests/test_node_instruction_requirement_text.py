"""Executable proof that the requirement's own words reach both prompts.

§3.6 B9 makes the reviewer's input a declared contract — goal, `produces`,
acceptance. §19 M1 recorded a projection that dropped the goal outright, and
`test_reviewer_contract_projection.py` closed that at the reviewer's own
projection. This file closes the same shape one projection earlier, at plan
ingress, where the goal was *populated* and therefore invisible: a lane's
title stood in for the requirement text that bounds the lane's scope, so every
consumer downstream faithfully relayed a summary.

The cost is on the record. Node `lane-p4-enrichment-ordering` of
`cmo-consolidation-l-r3` was authored from a 971-byte requirement and carried
199 bytes of it — the headline. The dropped remainder says the production
wiring is "explicitly out of scope for this lane and for this plan". In run
`run-2a44d226e75a4be391a14f02b78a6d25` its reviewer rejected the diff six
times on `diff.implements_the_stated_instruction`, for omitting exactly that
wiring, and the lane's whole review ceiling went with it. The plan author had
already asked and answered the reviewer's question; the sentence answering it
was dropped at ingress.

Measured across the four executable plans in that deployment, 51 agent nodes:
2,256 bytes of lane titles stood in for 18,824 bytes of `requirements[].text`.

What is settled here:

  B9    the requirement text that bounds a node's scope reaches the builder's
        prompt and the reviewer's contract, not a summary of it
  B9    the lane's title survives as a label rather than being replaced
  B13   the widened handoff is measured by the chokepoint check that already
        exists (`launcher.preflight_launch_prompt`), not by a second one
  §6.3  the text is plan content, so it is inside the plan's own bytes and
        therefore inside its digest — not a reference resolved elsewhere
  §1.2  nothing here decides a lifecycle transition; these are prompt bytes

The regression this file exists for is a narrowing: if the projection ever
returns to carrying the title alone, `test_the_instruction_is_not_the_title`
and every assertion below it fail.

Run with:  uv run adws/adw_test.py -k node_instruction_requirement_text
"""

from __future__ import annotations

import ast
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent
for _path in (str(ADWS), str(TESTS)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import maestro  # noqa: E402
from adw_modules import code_review as cr  # noqa: E402
from adw_modules import finalization as fin  # noqa: E402
from adw_modules import handoff_budget as hb  # noqa: E402
from adw_modules import launcher as lch  # noqa: E402
from adw_modules import plan_contract_ingress as pci  # noqa: E402
from adw_modules import plan_model as pm  # noqa: E402


#: The requirement of the real node that burned its review ceiling, in the
#: plan's own words. Held as a fixture rather than read from
#: `lexgenius-pipeline` so the proof survives in a checkout that has only this
#: repository. The final sentence is the one the reviewer never saw.
REAL_REQUIREMENT_ID = "req-p4-enrichment-ordering"
REAL_LANE_TITLE = (
    "Add the enrichment ordering gate admitting enrichment only after "
    "validation")
REAL_REQUIREMENT_TEXT = (
    "Section L Phase 4 step 4 of the MDL/CMO consolidation plan. Add "
    "src/lexgenius_pipeline/ingestion/judicial/cmo/enrichment_gate.py "
    "admitting markdown extraction, classification, deadline extraction, "
    "SALI tagging and leadership extraction only after binary and identity "
    "validation has completed for a document. A classification update never "
    "modifies an identity field. The gate is a pure policy function over "
    "injected enrichment callables: it owns the admission decision, not the "
    "call sites. Wiring the existing production enrichment callers — "
    "metadata_builder.py, scraper_connector.py, repair.py and the CLI — onto "
    "this gate is explicitly out of scope for this lane and for this plan, "
    "which declares none of those files as an output; a later plan owns that "
    "migration.")

#: The clause the six rejections turned on. Asserted on its own, because a
#: test that only compared byte counts would pass on a truncation that kept
#: the first paragraph and dropped this.
EXCLUSION_CLAUSE = "explicitly out of scope for this lane and for this plan"

_README = b"base\n"


def _ir() -> dict:
    """An approved IR whose one lane binds the real requirement above."""
    return {
        "schema_version": "plan-contract.v1",
        "plan_id": "phase-4",
        "title": "Phase 4",
        "plan_kind": "brownfield",
        "source_artifacts": [{
            "source_id": "src-readme",
            "path": "README.md",
            "sha256": hashlib.sha256(_README).hexdigest(),
            "required": True,
        }],
        "requirements": [{
            "requirement_id": REAL_REQUIREMENT_ID,
            "text": REAL_REQUIREMENT_TEXT,
            "surface": [
                {"path": "src/enrichment_gate.py", "mutation": "written"},
                {"path": "README.md", "mutation": "unmodified"},
            ],
            "effects": [],
        }],
        "lanes": [{
            "lane_id": "lane-p4-enrichment-ordering",
            "title": REAL_LANE_TITLE,
            "execution_context": ".",
            "requirement_ids": [REAL_REQUIREMENT_ID],
            "depends_on": [],
            "verifier_ids": ["verify-p4"],
        }],
        "verifiers": [{
            "verifier_id": "verify-p4",
            "lane_ids": ["lane-p4-enrichment-ordering"],
            "source_ids": ["src-readme"],
            "command": "python3 -m pytest tests/test_enrichment_gate.py",
            "min_executed": 3,
        }],
        "extensions": {"maestro": {
            "repo": "example",
            "outputs": {
                "lane-p4-enrichment-ordering": ["src/enrichment_gate.py"]},
            "prohibited_effects": [],
            "integration_branch": "main",
            "integration_gate": {
                "runner": "pytest", "argv": ["tests"], "cwd": ".",
                "min_cases": 1,
            },
        }},
    }


def _second_requirement(ir: dict) -> dict:
    """A lane that binds two requirements, which the fixture plans do not."""
    ir["requirements"].append({
        "requirement_id": "req-p4-ordering-audit",
        "text": "Record every admission decision with its deciding clause.",
        "surface": [{"path": "src/audit.py", "mutation": "written"}],
        "effects": [],
    })
    ir["lanes"][0]["requirement_ids"] = [
        REAL_REQUIREMENT_ID, "req-p4-ordering-audit"]
    ir["extensions"]["maestro"]["outputs"][
        "lane-p4-enrichment-ordering"].append("src/audit.py")
    return ir


class IngressFixture(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def project(self, mutate=None) -> dict:
        ir = _ir()
        if mutate is not None:
            mutate(ir)
        return pci.project_draft(ir, self.repo)

    def node(self, mutate=None) -> dict:
        return self.project(mutate)["nodes"][0]

    def instruction(self, mutate=None) -> str:
        return self.node(mutate)["instruction"]

    def refuses(self, code: str, mutate) -> str:
        with self.assertRaises(pci.IngressError) as caught:
            self.project(mutate)
        message = str(caught.exception)
        self.assertIn(code, message)
        return message


# ── the projection carries the requirement, not a headline for it ───────────

class TheRequirementTextIsProjectedTest(IngressFixture):
    """The narrowing this file exists to catch, asserted five ways."""

    def test_the_instruction_is_not_the_title(self):
        """The exact regression. `instruction == title` was true verbatim for
        12 of 12 agent nodes in the plan under test."""
        self.assertNotEqual(self.instruction(), REAL_LANE_TITLE)

    def test_the_requirement_text_is_carried_verbatim(self):
        self.assertIn(REAL_REQUIREMENT_TEXT, self.instruction())

    def test_the_clause_the_reviewer_rejected_on_is_carried(self):
        """A byte-count assertion would pass on a truncation that kept the
        first paragraph; this is the sentence that decided six reviews."""
        self.assertIn(EXCLUSION_CLAUSE, self.instruction())

    def test_the_title_survives_as_the_label(self):
        """Widened, not replaced: the headline still opens the brief."""
        self.assertTrue(self.instruction().startswith(REAL_LANE_TITLE))

    def test_the_requirement_is_labelled_by_its_own_id(self):
        """So a reader can tell a transcribed requirement from prose written
        for the prompt, and can find it in the plan it came from."""
        self.assertIn(
            pci.REQUIREMENT_HEADING.format(REAL_REQUIREMENT_ID),
            self.instruction())

    def test_the_instruction_is_larger_than_the_title_it_replaced(self):
        """The measured shape of the defect: 199 bytes carried of 971."""
        instruction = self.instruction()
        self.assertGreater(len(instruction), len(REAL_LANE_TITLE))
        self.assertGreaterEqual(
            len(instruction),
            len(REAL_LANE_TITLE) + len(REAL_REQUIREMENT_TEXT))


class EveryBoundRequirementIsCarriedTest(IngressFixture):
    """A lane binds a list, so carrying only the first is the same defect."""

    def test_both_requirements_reach_the_node(self):
        instruction = self.instruction(_second_requirement)
        self.assertIn(REAL_REQUIREMENT_TEXT, instruction)
        self.assertIn("Record every admission decision", instruction)

    def test_they_are_carried_in_the_order_the_lane_declares(self):
        instruction = self.instruction(_second_requirement)
        self.assertLess(
            instruction.index(REAL_REQUIREMENT_ID),
            instruction.index("req-p4-ordering-audit"))

    def test_a_duplicate_binding_is_carried_once(self):
        """The plan's bytes are its identity, so the instruction is a pure
        function of the IR — and a repeated id must not paste one requirement
        twice into the brief the reviewer reads."""
        def mutate(ir: dict) -> None:
            ir["lanes"][0]["requirement_ids"] = [
                REAL_REQUIREMENT_ID, REAL_REQUIREMENT_ID]
        instruction = self.instruction(mutate)
        self.assertEqual(instruction.count(REAL_REQUIREMENT_TEXT), 1)

    def test_the_projection_is_deterministic(self):
        """Authoring the same IR twice must produce the same bytes, or the
        plan digest is not a function of the plan (§6.3)."""
        self.assertEqual(self.instruction(_second_requirement),
                         self.instruction(_second_requirement))


# ── it refuses rather than falling back to the title ────────────────────────

class MissingRequirementTextRefusesTest(IngressFixture):
    """Every way the text can be unavailable, refused with the node named.

    A fallback to the title here would rebuild the defect: the projection
    would be silently lossy again, and — as with §19 M1 — the populated field
    would make it undetectable from anywhere downstream.
    """

    def test_a_binding_to_an_undeclared_requirement_refuses(self):
        def mutate(ir: dict) -> None:
            ir["lanes"][0]["requirement_ids"] = ["req-does-not-exist"]
        message = self.refuses("UNMAPPABLE_REQUIREMENTS", mutate)
        self.assertIn("req-does-not-exist", message)
        self.assertIn("lane-p4-enrichment-ordering", message)

    def test_a_requirement_without_text_refuses(self):
        def mutate(ir: dict) -> None:
            del ir["requirements"][0]["text"]
        self.refuses(
            "UNMAPPABLE_REQUIREMENTS:{}.text".format(REAL_REQUIREMENT_ID),
            mutate)

    def test_an_empty_requirement_text_refuses(self):
        def mutate(ir: dict) -> None:
            ir["requirements"][0]["text"] = ""
        self.refuses("UNMAPPABLE_REQUIREMENTS", mutate)

    def test_a_whitespace_only_requirement_text_refuses(self):
        """`_require_text` admits `"   "`; a heading with nothing under it is
        the same absence spelled differently."""
        def mutate(ir: dict) -> None:
            ir["requirements"][0]["text"] = "   \n\t "
        self.refuses(
            "UNMAPPABLE_REQUIREMENTS:{}.text".format(REAL_REQUIREMENT_ID),
            mutate)

    def test_a_non_string_requirement_text_refuses(self):
        def mutate(ir: dict) -> None:
            ir["requirements"][0]["text"] = ["a", "b"]
        self.refuses("UNMAPPABLE_REQUIREMENTS", mutate)

    def test_a_malformed_requirement_ids_refuses_as_a_lane_defect(self):
        """`"requirement_ids": "req-x"` is a string, and iterating it would
        spell out one phantom id per character — the `depends_on` defect this
        projection already refuses, on the field that now feeds the brief."""
        def mutate(ir: dict) -> None:
            ir["lanes"][0]["requirement_ids"] = REAL_REQUIREMENT_ID
        self.refuses(
            "UNMAPPABLE_LANES:lane-p4-enrichment-ordering.requirement_ids",
            mutate)

    def test_a_lane_binding_no_requirement_is_still_refused(self):
        """Not by this projection, and deliberately so: admission names the
        output no requirement claims, which is the defect. What matters for
        this file is that no such node ever reaches a plan carrying a
        title-only instruction — whichever check says so."""
        def mutate(ir: dict) -> None:
            ir["lanes"][0]["requirement_ids"] = []
        message = self.refuses("ADMISSION_REFUSED", mutate)
        self.assertIn("SURFACE_REACHABLE", message)


# ── the text reaches both halves of the contract ────────────────────────────

def _authored_plan(root: Path, repo: Path, ir: dict) -> "pm.Plan":
    """Drive the real authoring path, not a hand-built draft."""
    ir_path = root / "phase.plan.json"
    ir_path.write_text(json.dumps(ir), encoding="utf-8")
    receipt_path = root / "phase.plan-review.json"
    receipt_path.write_text(json.dumps({
        "schema_version": "plan-contract-review.v1",
        "verdict": "PASS",
        "ir_sha256": hashlib.sha256(ir_path.read_bytes()).hexdigest(),
    }), encoding="utf-8")
    stored, _trace = pci.author_from_plan_contract(
        ir_path, receipt_path, root / "maestro-plan.v1", repo)
    return pm.parse_bytes(stored)


def _repo(root: Path) -> Path:
    import subprocess

    repo = root / "repo"
    repo.mkdir()

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=str(repo), check=True,
                       capture_output=True, text=True)

    git("init", "-q", "-b", "main")
    git("config", "user.email", "maestro@example.invalid")
    git("config", "user.name", "Maestro Ingress")
    (repo / "README.md").write_bytes(_README)
    git("add", "-A")
    git("commit", "-q", "-m", "base")
    return repo


class BothPromptsCarryTheBoundingTextTest(unittest.TestCase):
    """B9 end to end, over the bytes each side is actually sent.

    Asserting on the projected node alone would be the M1 mistake in reverse:
    the field was populated there too, and the loss was one layer away.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.repo = _repo(self.root)
        self.plan = _authored_plan(self.root, self.repo, _ir())
        self.node = {node.node_id: node
                     for node in self.plan.to_plan_nodes()}[
                         "lane-p4-enrichment-ordering"]

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_the_builder_prompt_carries_the_requirement(self):
        prompt = maestro._agent_node_prompt(
            self.plan.node_by_id()["lane-p4-enrichment-ordering"],
            Path("/tmp/envelope.json"), None)
        self.assertIn(REAL_REQUIREMENT_TEXT, prompt)
        self.assertIn(EXCLUSION_CLAUSE, prompt)

    def test_the_reviewer_contract_carries_the_requirement(self):
        text = self._render()
        self.assertIn(REAL_REQUIREMENT_TEXT, text)
        self.assertIn(EXCLUSION_CLAUSE, text)

    def test_the_reviewers_goal_section_is_where_it_lands(self):
        """Under the goal heading specifically — B9's `goal`, not incidental
        text that happens to appear somewhere in the prompt."""
        text = self._render()
        heading = "## What this node was asked to do"
        self.assertIn(heading, text)
        goal = text.split(heading + "\n", 1)[1].split(
            "\n## Paths this node was permitted to write", 1)[0]
        self.assertIn(REAL_REQUIREMENT_TEXT, goal)

    def test_the_reviewers_goal_is_not_derived_from_the_gate(self):
        """§19 M1's placeholder must not reappear behind a widened field."""
        self.assertNotIn("Make the gate ", self._render())

    def test_the_goal_the_reviewer_reads_is_the_goal_the_builder_read(self):
        """One field, two prompts. Two sources would be two contracts, and a
        builder judged against a goal it was never given is B9 restated."""
        authored = self.plan.node_by_id()["lane-p4-enrichment-ordering"]
        self.assertEqual(authored.instruction, self.node.instruction)
        self.assertEqual(cr._node_goal(self.node), authored.instruction)
        self.assertTrue(
            maestro._agent_node_prompt(
                authored, Path("/tmp/envelope.json"), None
            ).startswith(authored.instruction))

    def _render(self) -> str:
        base, output = "a" * 40, "b" * 40
        objects = cr.review_objects(tuple(self.node.outputs), output)
        digest = cr.review_digest(
            run_id="run-87", node_id=self.node.node_id, base_sha=base,
            output_sha=output, rubric_version=cr.CODE_RUBRIC.version)
        matrix = fin.compute_matrix(cr.CODE_RUBRIC, digest, objects)
        return cr.build_handoff(
            subject_digest=digest, run_id="run-87", node=self.node,
            base_sha=base, output_sha=output,
            diff="--- a/x\n+++ b/x\n@@ -1 +1 @@\n-o\n+n\n",
            matrix=matrix, rubric=cr.CODE_RUBRIC,
            report_path=Path("/tmp/report.json")).render()


# ── B13: measured by the chokepoint that already exists ─────────────────────

class TheWidenedHandoffIsSizeCheckedTest(unittest.TestCase):
    """§3.6 B13, and §19 M6's correction to where it lives.

    Widening the instruction makes every prompt larger, so B13 has to be
    satisfied — but it is satisfied by the check that is already at the
    chokepoint, not by a second one next to this change. M6 records what a
    preflight installed on a path rather than at a chokepoint costs: coverage
    decays every time a route is added and nothing goes red. A second check
    here would be a second answer to one question.
    """

    def test_the_only_preflight_is_the_launcher_chokepoint(self):
        """`HerdrLauncher.launch` is above the process split, so every route
        crosses it. This asserts the call is still there rather than trusting
        the comment that says so."""
        source = (ADWS / "adw_modules" / "launcher.py").read_text(
            encoding="utf-8")
        tree = ast.parse(source, filename="launcher.py")
        callers = {
            function.name
            for function in ast.walk(tree)
            if isinstance(function, ast.FunctionDef)
            for call in ast.walk(function)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "preflight_launch_prompt"
        }
        self.assertIn("launch", callers)

    def test_the_arithmetic_has_exactly_one_definition(self):
        """`code_review` re-exports the leaf module's names rather than
        redefining them, so widening a prompt cannot be measured two ways."""
        self.assertIs(cr.estimate_tokens, hb.estimate_tokens)
        self.assertIs(cr.BYTES_PER_TOKEN, hb.BYTES_PER_TOKEN)
        self.assertIs(cr.route_publishes_a_window, hb.route_publishes_a_window)

    def test_a_widened_prompt_is_measured_by_that_check(self):
        """The number the chokepoint computes is the size of the file it is
        about to dispatch, so a longer instruction moves it."""
        title_only = hb.estimate_tokens(REAL_LANE_TITLE)
        widened = hb.estimate_tokens(
            REAL_LANE_TITLE + "\n\n"
            + pci.REQUIREMENT_HEADING.format(REAL_REQUIREMENT_ID) + "\n"
            + REAL_REQUIREMENT_TEXT)
        self.assertGreater(widened, title_only)

    def test_a_widened_reviewer_handoff_fails_closed_on_a_small_window(self):
        """The existing reviewer-side check, applied to the widened bytes.

        `preflight_launch_prompt` measures the dispatched file and is proved
        by `test_launch_prompt_preflight.py`; this proves the same rule holds
        of the text this change makes larger, using the one definition of the
        arithmetic rather than a copy of it.
        """
        widened = (
            REAL_LANE_TITLE + "\n\n"
            + pci.REQUIREMENT_HEADING.format(REAL_REQUIREMENT_ID) + "\n"
            + REAL_REQUIREMENT_TEXT)
        # A window whose *whole* size is the handoff's estimate. The budget is
        # half of it (`HANDOFF_CONTEXT_FRACTION`), so this refuses.
        window = hb.estimate_tokens(widened)
        with self.assertRaises(cr.HandoffTooLarge):
            cr.preflight_handoff(widened, window)
        self.assertEqual(cr.preflight_handoff(widened, 400_000),
                         hb.estimate_tokens(widened))


# ── prompt_assets: the channel this fix deliberately does not use ───────────

class PromptAssetsHasNoRuntimeReaderTest(unittest.TestCase):
    """Issue #87's third observation, recorded rather than built on.

    `prompt_assets` is empty on all 51 agent nodes across all four executable
    plans in the `lexgenius-pipeline` deployment. It is not a channel this
    projection forgot to write: it has no reader on the running side at all,
    which `plan_model._NODE_PROJECTION_EXEMPT` states in the code. A channel
    with no readers is §3.6 B15's field with no readers, and the repair for
    B15 is never to add a writer — it is to use a channel that has both ends.
    `instruction` has both, which is why this fix went there.

    This is a check, not a fix: it fails if someone gives `prompt_assets` a
    runtime reader without removing the exemption that says it has none, so
    the two cannot silently disagree.
    """

    def test_the_exemption_still_states_that_nothing_reads_one(self):
        reason = pm._NODE_PROJECTION_EXEMPT.get("prompt_assets")
        self.assertTrue(reason)
        self.assertIn("nothing on the scheduler's side reads one", reason)

    def test_the_scheduler_facing_node_carries_no_such_field(self):
        """The claim above, checked against the model rather than the
        comment: an exemption naming a field the projection does carry would
        be a stale reason nobody noticed."""
        from adw_modules import scheduler_types as st

        self.assertNotIn(
            "prompt_assets",
            {field.name for field in st.PlanNode.__dataclass_fields__.values()})

    def test_the_bounding_text_travels_on_a_channel_with_both_ends(self):
        """The positive form, and the reason this fix is not in the paragraph
        above: `instruction` is written by ingress and read by both prompt
        builders."""
        self.assertNotIn("instruction", pm._NODE_PROJECTION_EXEMPT)
        self.assertIn("instruction", pm.AgentNode.model_fields)


if __name__ == "__main__":
    unittest.main()
