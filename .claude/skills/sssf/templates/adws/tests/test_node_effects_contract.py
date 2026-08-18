"""The reviewer is told what the code inside a node may do.

Recorded failure, run-0120c32064d144c2aa55c344087e0b0a. The attempt-3 reviewer
prompt for `lane-p1-canonical-object-key` says, verbatim, "Make the gate
'pytest …' pass over selector '…', changing only the declared outputs." The
words the plan actually used about that node — "pure derivation", "object
mutation", "injected clients" — appear zero times in it. Against that brief an
executing object materializer *is* compliant, and the reviewers that found real
defects found them because that was the only work available to them.

Post-mirror the reviewer's contract is `instruction`, `declared_outputs`, the
gate command, its selector, `reads`, and `needs`. Every one answers *where*
work may happen. None answers *what the code inside it may do*, and even the
lane title the mirror restores says nothing about not calling an object store.

`effects` answers exactly that, as a closed enum over a closed enum. Handing
the reviewer the requirement's own text instead was declined deliberately: that
text says both "pure derivation and policy module" and "server-side copy it to
the canonical key", so it would put the reviewer in the builder's position
adjudicating a contradiction, and a verdict turning on which clause a model
weighted is §1.2's prose deciding a transition by the back door. Admission
removes the contradiction; this carries what survives it.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent
for _path in (str(ADWS), str(TESTS)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from adw_modules import code_review as cr              # noqa: E402
from adw_modules import finalization as fin            # noqa: E402
from adw_modules import plan_contract_ingress as pci   # noqa: E402
from adw_modules import plan_model as pm               # noqa: E402
from adw_modules import scheduler_types as st          # noqa: E402

from test_plan_admission import _ir, _requirement      # noqa: E402


COPY_MEANING = ("Writing or server-side copying a PDF to the canonical object "
                "key case-management-orders/sha256/{first2}/{sha256}.pdf.")


def _node(effects=()) -> st.PlanNode:
    return st.PlanNode(
        node_id="lane-canonical-object-key", kind=st.NodeKind.AGENT, depth=0,
        outputs=("src/canonical_object.py",),
        gate_command=("pytest", "tests/test_canonical_object.py"),
        gate_selector="tests/test_canonical_object.py", gate_min_cases=7,
        instruction="Derive the canonical object key.", effects=tuple(effects))


def _handoff(node: st.PlanNode) -> cr.ReviewHandoff:
    """The bytes a reviewer would actually be sent for this node.

    Built through the production rubric and matrix rather than a hand-made
    pair, so what is measured below is a real handoff.
    """
    base, output = "b" * 40, "c" * 40
    objects = cr.review_objects(tuple(node.outputs) or ("src/x.py",), output)
    digest = cr.review_digest(
        run_id="run-1", node_id=node.node_id, base_sha=base,
        output_sha=output, rubric_version=cr.CODE_RUBRIC.version)
    matrix = fin.compute_matrix(cr.CODE_RUBRIC, digest, objects)
    return cr.build_handoff(
        subject_digest=digest, run_id="run-1", node=node, base_sha=base,
        output_sha=output, diff="--- a/x\n+++ b/x\n@@ -1 +1 @@\n-o\n+n\n",
        matrix=matrix, rubric=cr.CODE_RUBRIC,
        report_path=Path("/tmp/report.json"))


class TheContractCarriesTheNodesEffectsTest(unittest.TestCase):
    def test_a_node_with_no_effects_renders_no_block(self):
        """An empty list renders nothing rather than an empty heading, so a
        reviewer is never shown a contract section with nothing under it."""
        rendered = _handoff(_node()).render()
        self.assertNotIn("## What this node may do", rendered)

    def test_the_block_names_the_act_the_disposition_and_the_meaning(self):
        effects = (pm.NodeEffect(effect="canonical_object_write",
                                 disposition="planned", meaning=COPY_MEANING),
                   pm.NodeEffect(effect="source_backfill",
                                 disposition="none",
                                 meaning="No artifact may be retrieved from a "
                                         "provider source by this plan."))
        rendered = _handoff(_node(effects)).render()
        self.assertIn("## What this node may do", rendered)
        self.assertIn("canonical_object_write: planned", rendered)
        self.assertIn("source_backfill: none", rendered)
        # The prohibition in the source document's own words. Without it the
        # plan reviewer and the node reviewer resolve one effect name against
        # two different documents.
        self.assertIn(COPY_MEANING, rendered)
        # Every disposition is defined in the block, so a reviewer never has
        # to guess what `planned` licenses.
        for word in ("performed", "planned", "fake_only", "none"):
            self.assertIn(word, rendered)

    def test_the_block_reaches_the_prompt_through_the_real_builder(self):
        """`build_handoff` is the one assembler, so this is the path a run
        takes rather than a hand-built model."""
        node = _node((pm.NodeEffect(effect="canonical_object_write",
                                    disposition="planned",
                                    meaning=COPY_MEANING),))
        handoff = _handoff(node)
        self.assertEqual(
            handoff.effects,
            [{"effect": "canonical_object_write", "disposition": "planned",
              "meaning": COPY_MEANING}])

    def test_the_effects_field_is_read_and_not_defaulted_around(self):
        """`getattr(node, "effects", ())` is what let `min_cases` and
        `instruction` read as their defaults for every node in every run, so
        the builder reads the attribute directly and a projection that stops
        carrying it fails loudly."""
        source = (ADWS / "adw_modules" / "code_review.py").read_text()
        self.assertNotIn('getattr(node, "effects"', source)


class TheProjectionCarriesEffectsTest(unittest.TestCase):
    def test_the_plan_node_projection_is_total_over_the_new_field(self):
        effect = pm.NodeEffect(effect="source_backfill", disposition="none",
                               meaning="No backfill.")
        node = pm.AgentNode(
            kind="agent", node_id="lane-a", instruction="do the work",
            outputs=("src/a.py",), effects=(effect,),
            gate=pm.Gate(runner="pytest", argv=("tests/test_a.py",), cwd=".",
                         min_cases=1))
        plan = pm.Plan(
            schema_version="maestro-plan.v1", plan_id="p", repo="r",
            base_commit="c" * 40, intent="i", evidence=(), nodes=(node,),
            merge_policy=pm.MergePolicy(
                integration_branch="main",
                integration_gate=pm.Gate(runner="pytest", argv=("tests",),
                                         cwd=".", min_cases=1)))
        projected = plan.to_plan_nodes()[0]
        self.assertEqual(projected.effects, (effect,))

    def test_a_plan_authored_before_the_field_still_parses(self):
        """The node default is not the optional-field-forever shape B8
        convicts: the authored field is `requirements[].effects` in the
        contract IR, which admission requires of every requirement. This
        default exists so an older `maestro-plan.v1` file still parses."""
        node = pm.AgentNode(
            kind="agent", node_id="lane-a", instruction="do the work",
            outputs=("src/a.py",),
            gate=pm.Gate(runner="pytest", argv=("tests/test_a.py",), cwd=".",
                         min_cases=1))
        self.assertEqual(node.effects, ())


class IngressFillsTheNodesEffectsTest(unittest.TestCase):
    """A lane's effects are the union of the requirements it binds.

    Admission has already refused two requirements on one lane that disagree
    about an effect, so the union is well defined by the time this runs.
    """

    def setUp(self) -> None:
        self.repo = Path(__file__).resolve().parent

    def node(self, draft, node_id):
        for node in draft["nodes"]:
            if node["node_id"] == node_id:
                return node
        raise AssertionError(node_id)

    def test_every_prohibited_effect_reaches_the_node_with_its_meaning(self):
        ir = _ir()
        draft = pci.project_draft(ir, self.repo)
        effects = self.node(draft, "lane-freeze")["effects"]
        self.assertEqual([entry["effect"] for entry in effects],
                         ["canonical_object_write", "source_backfill"])
        for entry in effects:
            self.assertEqual(entry["disposition"], "none")
            self.assertTrue(entry["meaning"].strip())

    def test_the_declared_disposition_is_carried_not_invented(self):
        ir = _ir()
        for entry in _requirement(ir, "req-freeze")["effects"]:
            if entry["effect"] == "canonical_object_write":
                entry["disposition"] = "fake_only"
        draft = pci.project_draft(ir, self.repo)
        carried = {entry["effect"]: entry["disposition"]
                   for entry in self.node(draft, "lane-freeze")["effects"]}
        self.assertEqual(carried["canonical_object_write"], "fake_only")
        self.assertEqual(carried["source_backfill"], "none")

    def test_a_lane_binding_several_requirements_takes_the_union(self):
        ir = _ir()
        ir["lanes"][1]["requirement_ids"] = ["req-freeze", "req-second"]
        ir["requirements"].append(
            {"requirement_id": "req-second", "text": "The same lane, again.",
             "surface": [{"path": "src/run_log.py", "mutation": "written"}],
             "effects": _requirement(ir, "req-freeze")["effects"]})
        draft = pci.project_draft(ir, self.repo)
        self.assertEqual(
            len(self.node(draft, "lane-freeze")["effects"]), 2)

    def test_an_effect_the_plan_does_not_prohibit_is_not_carried(self):
        """A disposition toward an act the plan permits is not a prohibition,
        and it has no transcribed meaning to render."""
        ir = _ir()
        _requirement(ir, "req-freeze")["effects"].append(
            {"effect": "migration_execution", "disposition": "performed"})
        draft = pci.project_draft(ir, self.repo)
        self.assertNotIn(
            "migration_execution",
            [entry["effect"] for entry in
             self.node(draft, "lane-freeze")["effects"]])


class TheBlockFitsTheReviewersWindowTest(unittest.TestCase):
    """B13 fails closed, so an over-budget block is a refusal rather than an
    overflow — but a refusal on every node would be just as unshippable."""

    #: The five prohibitions as the repaired IR actually transcribes them —
    #: 2,151 bytes of `meaning` across five effects, measured rather than
    #: assumed. The design estimate of ~280 bytes predates the `meaning`
    #: field, so the block is an order of magnitude larger than planned and
    #: the budget has to be checked against the real thing.
    MEANINGS = {
        "canonical_object_write": "x" * 523,
        "source_object_delete": "x" * 460,
        "catalog_projection_write": "x" * 365,
        "source_backfill": "x" * 405,
        "migration_execution": "x" * 398,
    }

    #: The largest reviewer prompt measured in a real run.
    LARGEST_REAL_PROMPT_BYTES = 43538

    def test_a_full_five_effect_block_fits_the_largest_real_handoff(self):
        effects = tuple(
            pm.NodeEffect(effect=effect, disposition="none", meaning=meaning)
            for effect, meaning in sorted(self.MEANINGS.items()))
        bare = _handoff(_node()).render()
        full = _handoff(_node(effects)).render()
        added = len(full) - len(bare)
        # A bound rather than an observation: B13 fails closed, so a block that
        # grew without anyone noticing would turn every node into a refusal.
        self.assertLess(added, 4096,
                        "the effects block grew to {0} bytes".format(added))
        # The largest real prompt, with the block on top, against the window
        # the reviewer route publishes. `preflight_handoff` raises rather than
        # returning false, so reaching the assertion is the check passing.
        padded = full + "x" * (self.LARGEST_REAL_PROMPT_BYTES - len(bare))
        self.assertGreaterEqual(len(padded), self.LARGEST_REAL_PROMPT_BYTES)
        cr.preflight_handoff(padded, context_window_tokens=200000)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
