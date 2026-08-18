"""The projection refuses rather than guessing — every field, not one.

`_project` is the sole path from the authoring IR to the executable plan, so a
value lost here is lost with nothing downstream able to notice: the plan is
canonical, digested, reviewed and finalized around whatever the projection
decided, and the IR takes no further part.

It lost values three ways, all spelled with the same operator:

1. **Two accepted spellings, silent precedence.** `integration.get("min_cases")
   or integration.get("min_executed")` picked one and never said which, and
   the gate's command admitted three overlapping forms in which `argv` meant
   two different things.
2. **A silent default the plan never declared.** `... or 1` gave the one gate
   that speaks for the whole tree a threshold of 1 — §7.4's item-44 failure,
   the one a run reached ACCEPTED through.
3. **`or` reading a deliberate falsy value as absence.** A plan declaring `0`,
   `""`, or `[]` was silently overridden with the fallback.

Every test here drives `project_draft`, which is the production projection. A
test that asserted on a hand-built draft would stay green with the projection
still guessing.
"""

from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent
for _path in (str(ADWS), str(TESTS)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from adw_modules import plan_contract_ingress as pci


def _ir() -> dict:
    """The smallest IR that projects. Every refusal below is one edit to it."""
    return {
        "schema_version": "plan-contract.v1",
        "plan_id": "phase-1",
        "title": "Phase 1 freeze",
        "plan_kind": "brownfield",
        "source_artifacts": [{
            "source_id": "src-readme",
            "path": "README.md",
            "sha256": "a" * 64,
            "required": True,
        }],
        "requirements": [{
            "requirement_id": "req-freeze",
            "text": "Freeze the writers behind a greeting module.",
            # Where the requirement's behaviour lives, declared as paths and
            # mutation kinds rather than left to the prose above. `written`
            # must be one of this lane's own outputs; `unmodified` must be a
            # pinned source artifact no lane rewrites.
            "surface": [
                {"path": "src/greeting.py", "mutation": "written"},
                {"path": "README.md", "mutation": "unmodified"},
            ],
            # Required, like `surface`: a plan states what external acts it
            # forbids and every requirement states its disposition toward each
            # one. This fixture forbids nothing, which is a declaration rather
            # than an omission.
            "effects": [],
        }],
        "lanes": [{
            "lane_id": "lane-freeze",
            "title": "Freeze writers",
            "execution_context": ".",
            "requirement_ids": ["req-freeze"],
            "depends_on": [],
            "verifier_ids": ["verify-freeze"],
        }],
        "verifiers": [{
            "verifier_id": "verify-freeze",
            "lane_ids": ["lane-freeze"],
            "source_ids": ["src-readme"],
            "command": "python3 -m pytest tests/test_existing.py",
            "min_executed": 1,
        }],
        "extensions": {"maestro": {
            "repo": "example",
            "outputs": {"lane-freeze": ["src/greeting.py"]},
            "prohibited_effects": [],
            "integration_branch": "main",
            "integration_gate": {
                "runner": "pytest", "argv": ["tests"], "cwd": ".",
                "min_cases": 1,
            },
        }},
    }


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

    def gate(self, draft: dict) -> dict:
        return draft["merge_policy"]["integration_gate"]

    def refuses(self, code: str, mutate) -> None:
        with self.assertRaises(pci.IngressError) as caught:
            self.project(mutate)
        self.assertIn(code, str(caught.exception))

    def test_the_unmodified_fixture_projects(self):
        """The control. Without it every refusal below could be passing for
        the wrong reason."""
        draft = self.project()
        self.assertEqual(self.gate(draft)["min_cases"], 1)
        self.assertEqual(draft["nodes"][0]["node_id"], "lane-freeze")


def _integration(ir: dict) -> dict:
    return ir["extensions"]["maestro"]["integration_gate"]


class Shape1SilentPrecedenceTest(IngressFixture):
    """Two spellings for one fact, and nothing says which won."""

    def test_both_threshold_spellings_at_once_is_refused(self):
        def mutate(ir):
            _integration(ir).update(min_cases=5, min_executed=70)
        self.refuses("UNMAPPABLE_INTEGRATION:min_cases", mutate)

    def test_either_threshold_spelling_alone_is_honoured(self):
        """Neither key is privileged — the refusal is for declaring both, not
        for choosing the unfashionable one."""
        for key in ("min_cases", "min_executed"):
            with self.subTest(key=key):
                def mutate(ir, key=key):
                    gate = _integration(ir)
                    gate.pop("min_cases", None)
                    gate[key] = 70
                self.assertEqual(self.gate(self.project(mutate))["min_cases"], 70)

    def test_both_command_spellings_at_once_is_refused(self):
        def mutate(ir):
            _integration(ir)["command"] = "pytest tests"
        self.refuses("UNMAPPABLE_INTEGRATION:runner-or-command", mutate)

    def test_a_command_line_may_not_also_carry_an_argv(self):
        """`argv` meant the selector beside a `runner` and the whole command
        including its binary without one. Carrying both makes that ambiguity
        unresolvable rather than quiet."""
        def mutate(ir):
            gate = _integration(ir)
            gate.pop("runner")
            gate["command"] = "pytest tests"
        self.refuses("UNMAPPABLE_INTEGRATION:command-and-argv", mutate)

    def test_a_command_line_alone_still_projects(self):
        def mutate(ir):
            gate = _integration(ir)
            gate.pop("runner")
            gate.pop("argv")
            gate["command"] = "pytest tests"
        gate = self.gate(self.project(mutate))
        self.assertEqual((gate["runner"], gate["argv"]), ("pytest", ["tests"]))


class Shape2SilentDefaultTest(IngressFixture):
    """A value the plan never declared, invented at the boundary."""

    def test_an_integration_gate_declaring_no_threshold_is_refused(self):
        """Not adjudicated at 1. §7.4 item 44 is the run that reached ACCEPTED
        because this defaulted."""
        def mutate(ir):
            _integration(ir).pop("min_cases")
        self.refuses("UNMAPPABLE_INTEGRATION:min_cases", mutate)

    def test_a_lane_without_a_title_is_refused(self):
        """The title becomes the agent's whole instruction. Defaulting it hands
        an agent its own node id as the brief."""
        def mutate(ir):
            ir["lanes"][0].pop("title")
        self.refuses("UNMAPPABLE_LANES:lane-freeze.title", mutate)

    def test_a_source_that_does_not_say_it_is_required_is_refused(self):
        """`required` had no reader, so an optional source was silently
        promoted and then refused downstream as a missing file."""
        def mutate(ir):
            ir["source_artifacts"][0].pop("required")
        self.refuses("UNMAPPABLE_SOURCES:src-readme.required", mutate)

    def test_an_optional_source_is_refused_loudly(self):
        """Maestro's `Observed` evidence has no optional form (§12.3)."""
        def mutate(ir):
            ir["source_artifacts"][0]["required"] = False
        self.refuses("UNMAPPABLE_SOURCES:src-readme.required", mutate)

    def test_a_plan_without_a_title_is_refused(self):
        def mutate(ir):
            ir.pop("title")
        self.refuses("IR_SCHEMA:title", mutate)

    def test_an_absent_repo_is_left_for_the_one_layer_that_resolves_it(self):
        """`plan_author.fill_git_facts` already fills an absent `repo` from the
        repository it is handed. Filling it here as well is one fact in two
        places — so the key is omitted, not duplicated, and an empty one is a
        malformed value rather than an omission."""
        def absent(ir):
            ir["extensions"]["maestro"].pop("repo")
        self.assertNotIn("repo", self.project(absent))

        def empty(ir):
            ir["extensions"]["maestro"]["repo"] = ""
        self.refuses("MAESTRO_EXTENSION_MISSING:repo", empty)

    def test_an_absent_integration_cwd_is_the_repository_root(self):
        """The one default judged legitimate: §8.8 runs this gate once over the
        integrated tree, and the root is the only place that tree is whole.
        Keyed on the key's absence, never on its falsiness."""
        def mutate(ir):
            _integration(ir).pop("cwd")
        self.assertEqual(self.gate(self.project(mutate))["cwd"], ".")


class Shape3FalsyIsNotAbsenceTest(IngressFixture):
    """A plan that deliberately says `0`, `""` or `[]` said something."""

    def test_a_threshold_of_zero_is_refused_not_replaced(self):
        """Under `or` this read as absence and became 1 — a gate demanding
        nothing silently became a gate demanding one case."""
        def mutate(ir):
            _integration(ir)["min_cases"] = 0
        self.refuses("UNMAPPABLE_INTEGRATION:min_cases", mutate)

    def test_a_boolean_threshold_is_refused(self):
        """`min_cases: true` is a typo, not a demand for one case, and Python
        would otherwise agree that it is the integer 1."""
        def mutate(ir):
            _integration(ir)["min_cases"] = True
        self.refuses("UNMAPPABLE_INTEGRATION:min_cases", mutate)

    def test_an_empty_integration_cwd_is_refused_not_defaulted(self):
        def mutate(ir):
            _integration(ir)["cwd"] = ""
        self.refuses("UNMAPPABLE_INTEGRATION:cwd", mutate)

    def test_an_empty_runner_is_refused_not_reparsed(self):
        """`if integration.get("runner")` sent an empty runner down the
        command-parsing branch, where it met an `argv` that meant something
        else."""
        def mutate(ir):
            _integration(ir)["runner"] = ""
        self.refuses("UNMAPPABLE_INTEGRATION:runner", mutate)

    def test_an_empty_lane_title_is_refused(self):
        def mutate(ir):
            ir["lanes"][0]["title"] = ""
        self.refuses("UNMAPPABLE_LANES:lane-freeze.title", mutate)


class MalformedIsNotAbsenceTest(IngressFixture):
    """The same operator's quieter damage: a wrong *type* absorbed silently."""

    def test_a_string_dependency_is_refused_not_spelled_out(self):
        """`list("lane-a")` is six phantom node ids, and `or` was happy with
        it because a non-empty string is truthy."""
        def mutate(ir):
            ir["lanes"][0]["depends_on"] = "lane-a"
        self.refuses("UNMAPPABLE_LANES:lane-freeze.depends_on", mutate)

    def test_an_absent_dependency_list_is_still_no_dependencies(self):
        """A root lane genuinely has none, so absence stays legal — what is
        refused is a malformed value, not an omitted one."""
        def mutate(ir):
            ir["lanes"][0].pop("depends_on")
        self.assertEqual(self.project(mutate)["nodes"][0]["needs"], [])

    def test_a_non_string_source_read_is_refused_not_dropped(self):
        """A filtered comprehension deleted it, so evidence the lane declared
        it reads vanished from `reads` with nothing said."""
        def mutate(ir):
            ir["verifiers"][0]["source_ids"] = ["src-readme", 7]
        self.refuses("UNMAPPABLE_VERIFIERS:lane-freeze.source_ids", mutate)

    def test_a_source_without_an_id_refuses_rather_than_raising_keyerror(self):
        """`source["source_id"]` was a bare index, so this boundary leaked an
        untyped `KeyError` instead of naming what was unmappable."""
        def mutate(ir):
            ir["source_artifacts"][0].pop("source_id")
        self.refuses("UNMAPPABLE_SOURCES:source[0].source_id", mutate)


class ThePinIsCarriedNotDroppedTest(IngressFixture):
    """`docs/plan-authoring.md` makes a hash-pinned `source_artifacts` entry
    the only way a document enters the pipeline. The projection dropped the
    hash, so `plan_author.fill_git_facts` filled `Observed.sha256` from the
    repository and the IR's declaration was never compared to anything.
    """

    def test_the_declared_digest_reaches_the_projected_evidence(self):
        observed = [item for item in self.project()["evidence"]
                    if item["kind"] == "observed"]
        self.assertEqual([item["sha256"] for item in observed], ["a" * 64])

    def test_a_source_without_a_digest_is_refused(self):
        def mutate(ir):
            ir["source_artifacts"][0].pop("sha256")
        self.refuses("UNMAPPABLE_SOURCES:src-readme.sha256", mutate)

    def test_a_malformed_digest_is_refused(self):
        for value in ("a" * 63, "a" * 65, "A" * 64, "z" * 64, "", 1):
            with self.subTest(value=value):
                def mutate(ir, value=value):
                    ir["source_artifacts"][0]["sha256"] = value
                self.refuses("UNMAPPABLE_SOURCES:src-readme.sha256", mutate)


class TheRefusalNamesTheMalformedObjectTest(IngressFixture):
    """A verifier's own defect must name the verifier, not the lane.

    `lane_id in (item.get("lane_ids") or [])` absorbed a malformed binding into
    "matches no lane", so the lane refused for having no verifier. Fail-closed,
    but pointing at the wrong object — which is how a plan defect gets read as
    a missing binding and edited in the wrong place.
    """

    def test_a_string_lane_binding_names_the_verifier(self):
        def mutate(ir):
            ir["verifiers"][0]["lane_ids"] = "lane-freeze"
        self.refuses("UNMAPPABLE_VERIFIERS:verify-freeze.lane_ids", mutate)

    def test_a_verifier_bound_to_nothing_names_the_verifier(self):
        def mutate(ir):
            ir["verifiers"][0]["lane_ids"] = []
        self.refuses("UNMAPPABLE_VERIFIERS:verify-freeze.lane_ids", mutate)

    def test_a_verifier_without_an_id_is_refused(self):
        def mutate(ir):
            ir["verifiers"][0].pop("verifier_id")
        self.refuses("UNMAPPABLE_VERIFIERS:verifier[0].verifier_id", mutate)

    def test_a_non_object_verifier_is_refused_not_skipped(self):
        def mutate(ir):
            ir["verifiers"].append("not-a-verifier")
        self.refuses("UNMAPPABLE_VERIFIERS:verifier[1]", mutate)


class TheIntegrationGateCwdIsCheckedTest(IngressFixture):
    """Every lane cwd was refused `AMBIENT_PATH`; the integration gate's was
    never looked at, so it was the one path on which `..` reached a plan."""

    def test_an_ambient_integration_cwd_is_refused(self):
        def mutate(ir):
            _integration(ir)["cwd"] = "../elsewhere"
        self.refuses("AMBIENT_PATH:integration_gate", mutate)

    def test_an_absolute_integration_cwd_is_refused(self):
        def mutate(ir):
            _integration(ir)["cwd"] = "/etc"
        self.refuses("AMBIENT_PATH:integration_gate", mutate)


class NoSilentDefaultsRemainTest(unittest.TestCase):
    """The class guard. `x.get(...) or <fallback>` is the shape this file
    removed, and the projection is where it costs the most.

    A judged-legitimate default belongs in a typed, visible branch keyed on the
    key's absence — `if "cwd" in integration` — never in an `or` chain, because
    an `or` chain cannot distinguish the three cases above from one another.
    """

    def test_the_projection_contains_no_or_fallback_chains(self):
        import ast

        source = (ADWS / "adw_modules"
                  / "plan_contract_ingress.py").read_text(encoding="utf-8")
        tree = ast.parse(source, filename="plan_contract_ingress.py")
        offenders = []
        for function in ast.walk(tree):
            if not isinstance(function, ast.FunctionDef):
                continue
            if function.name != "project_draft":
                continue
            for node in ast.walk(function):
                if not isinstance(node, ast.BoolOp):
                    continue
                if not isinstance(node.op, ast.Or):
                    continue
                # `a or b` is only this defect when the left side is a lookup
                # that can be absent. `not isinstance(x, str) or not x` is a
                # validity test and is exactly what replaced the chains.
                first = node.values[0]
                looked_up = (
                    isinstance(first, ast.Call)
                    and isinstance(first.func, ast.Attribute)
                    and first.func.attr == "get"
                ) or isinstance(first, ast.Subscript)
                if looked_up:
                    offenders.append(
                        "line {}: {}".format(node.lineno,
                                             ast.unparse(node)))
        self.assertEqual(
            [], offenders,
            "an `or` fallback over a lookup in the projection. It cannot tell "
            "an absent key from a deliberate `0`/`\"\"`/`[]`, and it invents a "
            "value the plan never declared. Branch on `key in mapping` and "
            "refuse with a typed code instead.\n" + "\n".join(offenders))


if __name__ == "__main__":
    unittest.main()
