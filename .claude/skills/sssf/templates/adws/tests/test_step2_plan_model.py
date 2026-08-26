"""Executable proof of §6.2 and §6.3 — the plan model, the three evidence
types, canonical bytes, and the digest taken over stored bytes.

The properties settled here are the ones a reading cannot settle:

  §6.2  nine in-plan types, and exactly three evidence types
  §6.2  evidence is a discriminated union, so a type cannot be mislabelled
  §6.2  `Hypothesis` is structurally incapable of carrying a path, a sha, or
        a producer — `extra=forbid` refuses them rather than ignoring them
  §6.2  an agent node's gate is required; a code node's acceptance is its
        exit code, so a code node has no gate field to carry one
  §6.2  retry budgets have no field anywhere in the plan
  §6.3  canonicalization is stable, and `digest_of` takes stored bytes
  §6.3  prompt-asset digests are inputs to the plan digest
  §6.3  nothing is excluded from the digest, because non-semantic data has
        no field and `extra=forbid` rejects it at parse
  §6.3  schema evolution is an append-only registry with no upgrade function
  §6.3  the import boundary: nothing on the runtime path can re-canonicalize

The last of those is Step 2's shipped invariant (§12.2). It is checked by
parsing every module in the tree rather than by grepping for a string,
because an import is a fact about a module and a grep is a fact about text.

Run with:  uv run adws/adw_test.py -k plan_model
"""

from __future__ import annotations

import ast
import json
import sys
import unittest
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
if str(ADWS) not in sys.path:
    sys.path.insert(0, str(ADWS))

from adw_modules import plan_canonical as pc  # noqa: E402
from adw_modules import plan_digest as pd  # noqa: E402
from adw_modules import plan_model as pm  # noqa: E402
from adw_modules import scheduler_types as st  # noqa: E402


# ── a minimal plan, built as a mapping so the tests can corrupt one field ────

def plan_mapping() -> dict:
    """The smallest plan that parses: one agent node, one code node."""
    return {
        "schema_version": "maestro-plan.v1",
        "plan_id": "plan-001",
        "repo": "example/repo",
        "base_commit": "0" * 40,
        "intent": "add a greeting",
        "evidence": [
            {"kind": "observed", "evidence_id": "e-readme",
             "path": "README.md", "sha256": "a" * 64},
            {"kind": "produced", "evidence_id": "e-greeting",
             "path": "src/greeting.py", "producer": "n-write", "base_sha256": None},
            {"kind": "hypothesis", "evidence_id": "e-guess",
             "statement": "the greeting belongs beside the entrypoint"},
        ],
        "nodes": [
            {"kind": "agent", "node_id": "n-write", "needs": [],
             "reads": ["e-readme", "e-guess"], "outputs": ["src/greeting.py"],
             "instruction": "write the greeting",
             "gate": {"runner": "pytest", "cwd": ".",
                      "argv": ["tests/test_greeting.py"], "min_cases": 1},
             "prompt_assets": [
                 {"role": "system", "path": "prompts/write.md", "sha256": "b" * 64}]},
            {"kind": "code", "node_id": "n-suite", "needs": ["n-write"],
             "reads": ["e-greeting"], "outputs": [],
             "command": ["pytest", "-q"], "cwd": ".", "expects_changes": False},
        ],
        "merge_policy": {
            "integration_branch": "main",
            "integration_gate": {"runner": "pytest", "cwd": ".",
                                 "argv": ["tests"], "min_cases": 1},
        },
        "supersedes": None,
    }


def stored_bytes(mapping: dict) -> bytes:
    """Canonical stored bytes for a mapping — what an author would write."""
    return pc.canonicalize(pm.parse_mapping(mapping))


class ModelShapeTests(unittest.TestCase):
    def test_ten_in_plan_types(self):
        """§6.2 shipped with nine. `NodeEffect` is the tenth, `TestsNode`
        the eleventh. The count is asserted rather than described for the
        reason §6.2 gives about every other count in this design: a number
        in a docstring drifts, and a member added without a decision is how
        a closed set stops being one.

        Five more arrived with `maestro-plan.v4`'s test-strength contract:
        `TestsNodeV4` and the four types that contract is made of. They are
        listed here rather than exempted because the whole value of this
        assertion is that adding a type is a decision somebody records.

        MAESTRO_architecture.md §6.2 still reads "nine in-plan types" and has
        to be brought level with this; the count here is the enforced one.
        """
        self.assertEqual(len(pm.IN_PLAN_TYPES), 16)
        self.assertEqual(
            {cls.__name__ for cls in pm.IN_PLAN_TYPES},
            {"Plan", "Observed", "Produced", "Hypothesis", "Gate",
             "AgentNode", "CodeNode", "TestsNode", "TestsNodeV4",
             "MergePolicy", "PromptAsset", "NodeEffect",
             "CoverageObligation", "ControlledMutation", "Falsifiability",
             "TestStrength"})

    def test_exactly_three_evidence_types(self):
        self.assertEqual(len(pm.EVIDENCE_TYPES), 3)
        self.assertEqual({cls.__name__ for cls in pm.EVIDENCE_TYPES},
                         {"Observed", "Produced", "Hypothesis"})

    def test_the_minimal_plan_parses(self):
        plan = pm.parse_mapping(plan_mapping())
        self.assertEqual(plan.schema_version, pm.SCHEMA_V1)
        self.assertEqual(len(plan.nodes), 2)
        self.assertEqual(len(plan.evidence), 3)

    def test_evidence_is_discriminated_not_an_enum(self):
        """A mislabelled type is refused at parse, not accepted and re-read."""
        data = plan_mapping()
        # A `Produced` body wearing the `observed` label: the discriminator
        # sends it to Observed, where `producer` is not a field at all.
        data["evidence"][1] = {"kind": "observed", "evidence_id": "e-greeting",
                               "path": "src/greeting.py", "producer": "n-write",
                               "base_sha256": None}
        with self.assertRaises(pm.PlanParseError):
            pm.parse_mapping(data)

    def test_hypothesis_cannot_carry_a_path_a_sha_or_a_producer(self):
        for extra in ({"path": "src/x.py"}, {"sha256": "c" * 64},
                      {"producer": "n-write"}):
            data = plan_mapping()
            data["evidence"][2].update(extra)
            with self.assertRaises(pm.PlanParseError):
                pm.parse_mapping(data)

    def test_an_agent_node_must_carry_a_gate(self):
        data = plan_mapping()
        del data["nodes"][0]["gate"]
        with self.assertRaises(pm.PlanParseError):
            pm.parse_mapping(data)

    def test_a_code_node_has_no_gate_field_to_carry_one(self):
        data = plan_mapping()
        data["nodes"][1]["gate"] = {"runner": "pytest", "cwd": ".",
                                    "argv": ["tests"], "min_cases": 1}
        with self.assertRaises(pm.PlanParseError):
            pm.parse_mapping(data)

    def test_a_gate_requires_min_cases_of_at_least_one(self):
        data = plan_mapping()
        data["nodes"][0]["gate"]["min_cases"] = 0
        with self.assertRaises(pm.PlanParseError):
            pm.parse_mapping(data)

    def test_the_plain_argv_arm_is_deleted_for_agent_nodes(self):
        """§6.2 — a gate names a runner from a closed set, never a bare argv."""
        data = plan_mapping()
        data["nodes"][0]["gate"]["runner"] = "make"
        with self.assertRaises(pm.PlanParseError):
            pm.parse_mapping(data)

    def test_no_field_anywhere_expresses_a_retry_budget(self):
        """§6.2 — retry budgets are operational configuration. A field for one
        would mint a new digest every time a crash budget was tuned."""
        names = set()
        for cls in pm.IN_PLAN_TYPES:
            names.update(cls.model_fields)
        for forbidden in ("retries", "retry", "retry_budget", "max_attempts",
                          "attempts", "semantic_ceiling", "timeout"):
            self.assertNotIn(forbidden, names)

    def test_non_semantic_data_has_no_field_and_is_refused_at_parse(self):
        """§6.3 — there is no excluded channel to smuggle semantics through."""
        for key, value in (("created_at", "2026-08-13"), ("author", "ada"),
                           ("route", "omp"), ("review", {"verdict": "PASS"})):
            data = plan_mapping()
            data[key] = value
            with self.assertRaises(pm.PlanParseError):
                pm.parse_mapping(data)

    def test_supersedes_must_be_a_canonical_receipt_digest_at_parse_time(self):
        for supersedes in ("A" * 64, "a" * 63, "not-a-digest"):
            with self.subTest(supersedes=supersedes):
                data = plan_mapping()
                data["supersedes"] = supersedes
                with self.assertRaises(pm.PlanParseError) as caught:
                    pm.parse_mapping(data)
                self.assertEqual(caught.exception.pointers[0][0], "/supersedes")


class NodeProjectionTests(unittest.TestCase):
    """§6.2 — nodes are consumed directly; there is no second authored type."""

    def test_nodes_project_onto_the_schedulers_own_node(self):
        plan = pm.parse_mapping(plan_mapping())
        nodes = plan.to_plan_nodes()
        self.assertTrue(all(isinstance(n, st.PlanNode) for n in nodes))
        by_id = {n.node_id: n for n in nodes}
        self.assertEqual(by_id["n-write"].kind, st.NodeKind.AGENT)
        self.assertEqual(by_id["n-write"].depth, 0)
        self.assertEqual(by_id["n-suite"].kind, st.NodeKind.CODE)
        self.assertEqual(by_id["n-suite"].depth, 1)
        self.assertEqual(by_id["n-suite"].needs, ("n-write",))
        self.assertEqual(by_id["n-write"].outputs, ("src/greeting.py",))

    def test_the_projection_carries_the_agent_nodes_own_selector(self):
        plan = pm.parse_mapping(plan_mapping())
        node = {n.node_id: n for n in plan.to_plan_nodes()}["n-write"]
        self.assertEqual(node.gate_selector, "tests/test_greeting.py")
        self.assertIn("pytest", node.gate_command)

    def test_a_selectorless_agent_gate_is_refused_by_the_projection(self):
        """§7.4's deadlock is refused where the node is built, not at run."""
        data = plan_mapping()
        data["nodes"][0]["gate"]["argv"] = ["-q"]
        plan = pm.parse_mapping(data)
        with self.assertRaises(ValueError):
            plan.to_plan_nodes()


class SelectorAndCommandCoreTests(unittest.TestCase):
    """§6.4 — command core := (runner, cwd, argv normalized)."""

    def gate(self, argv, cwd=".", runner="pytest"):
        return pm.Gate(runner=runner, cwd=cwd, argv=tuple(argv), min_cases=1)

    def test_a_bare_argv_names_no_selector(self):
        self.assertIsNone(pm.selector_of(self.gate(["-q", "--color=no"])))

    def test_selector_flags_and_their_values_are_part_of_the_selector(self):
        self.assertEqual(pm.selector_of(self.gate(["-k", "greeting"])),
                         "-k greeting")

    def test_a_reordered_flag_does_not_change_the_command_core(self):
        a = pm.command_core(self.gate(["-q", "tests/test_a.py", "--tb=short"]))
        b = pm.command_core(self.gate(["--tb=short", "tests/test_a.py", "-q"]))
        self.assertEqual(a, b)

    def test_noise_flags_do_not_change_the_command_core(self):
        a = pm.command_core(self.gate(["tests/test_a.py"]))
        b = pm.command_core(self.gate(["-q", "-v", "--color=no", "tests/test_a.py"]))
        self.assertEqual(a, b)

    def test_a_different_selector_is_a_different_core(self):
        a = pm.command_core(self.gate(["tests/test_a.py"]))
        b = pm.command_core(self.gate(["tests/test_b.py"]))
        self.assertNotEqual(a, b)

    def test_cwd_and_runner_are_part_of_the_core(self):
        a = pm.command_core(self.gate(["tests/test_a.py"]))
        self.assertNotEqual(a, pm.command_core(self.gate(["tests/test_a.py"],
                                                         cwd="packages/web")))
        self.assertNotEqual(a, pm.command_core(self.gate(["tests/test_a.py"],
                                                         runner="vitest")))

    def test_unscoped_argv_drops_paths_and_selector_flags(self):
        """The integration gate's executed argv is whole-tree collection."""
        self.assertEqual(
            pm.unscoped_argv(["-q", "tests/a.py", "tests/b.py"]),
            ("-q",))
        self.assertEqual(
            pm.unscoped_argv(["-k", "greeting", "--tb=short", "tests"]),
            ("--tb=short",))
        self.assertEqual(pm.unscoped_argv(["-q"]), ("-q",))
        self.assertEqual(pm.unscoped_argv(["tests"]), ())
        self.assertNotIn("-o", pm.unscoped_argv(["-q", "tests"]))
        self.assertNotIn("addopts=", pm.unscoped_argv(["-q", "tests"]))


class CanonicalBytesTests(unittest.TestCase):
    def test_canonicalization_is_stable(self):
        plan = pm.parse_mapping(plan_mapping())
        self.assertEqual(pc.canonicalize(plan), pc.canonicalize(plan))

    def test_key_order_in_the_authored_file_does_not_change_the_digest(self):
        first = stored_bytes(plan_mapping())
        shuffled = dict(reversed(list(plan_mapping().items())))
        self.assertEqual(pd.digest_of(first), pd.digest_of(stored_bytes(shuffled)))

    def test_stored_bytes_round_trip_through_the_parser_unchanged(self):
        stored = stored_bytes(plan_mapping())
        self.assertEqual(pc.canonicalize(pm.parse_bytes(stored)), stored)

    def test_a_plan_file_written_before_title_stays_canonical(self):
        """Dumping None as `"title":null` would mint a new identity for every
        already-shipped file. Absent stays absent."""
        stored = stored_bytes(plan_mapping())
        self.assertNotIn(b'"title"', stored)
        self.assertTrue(pc.is_canonical(stored))
        self.assertIsNone(pm.parse_bytes(stored).title)
        self.assertEqual(pc.canonicalize(pm.parse_bytes(stored)), stored)

    def test_non_canonical_bytes_are_detectable_without_being_rewritten(self):
        stored = stored_bytes(plan_mapping())
        loose = json.dumps(json.loads(stored.decode("utf-8")), indent=2).encode("utf-8")
        self.assertFalse(pc.is_canonical(loose))
        self.assertNotEqual(pd.digest_of(loose), pd.digest_of(stored))

    def test_the_digest_is_a_function_of_bytes_alone(self):
        stored = stored_bytes(plan_mapping())
        self.assertEqual(pd.digest_of(stored),
                         pd.digest_of(bytes(bytearray(stored))))
        self.assertEqual(len(pd.digest_of(stored)), 64)

    def test_a_prompt_asset_digest_is_an_input_to_the_plan_digest(self):
        """§6.3 — editing a prompt mints a different plan digest, which finds
        no receipt, which refuses the run."""
        data = plan_mapping()
        before = pd.digest_of(stored_bytes(data))
        data["nodes"][0]["prompt_assets"][0]["sha256"] = "c" * 64
        self.assertNotEqual(before, pd.digest_of(stored_bytes(data)))

    def test_every_semantic_field_moves_the_digest(self):
        base = pd.digest_of(stored_bytes(plan_mapping()))
        mutations = [
            ("intent", lambda d: d.update(intent="something else")),
            ("plan_id", lambda d: d.update(plan_id="plan-002")),
            ("base_commit", lambda d: d.update(base_commit="1" * 40)),
            ("gate", lambda d: d["nodes"][0]["gate"].update(min_cases=2)),
            ("evidence", lambda d: d["evidence"][0].update(sha256="d" * 64)),
            ("merge_policy", lambda d: d["merge_policy"].update(
                integration_branch="trunk")),
            ("supersedes", lambda d: d.update(supersedes="e" * 64)),
        ]
        for name, mutate in mutations:
            data = plan_mapping()
            mutate(data)
            self.assertNotEqual(base, pd.digest_of(stored_bytes(data)),
                                f"{name} did not move the digest")


class SchemaRegistryTests(unittest.TestCase):
    def test_v1_dispatches_to_the_frozen_v1_model(self):
        self.assertIs(pm.parser_for(pm.SCHEMA_V1), pm.Plan)

    def test_v2_dispatches_to_its_own_class_not_to_the_v1_one(self):
        """§6.3's registry, exercised by the second entry it has ever had.

        v2 is structurally identical to v1 — the difference is what the
        projection puts in `instruction` (§19 M26) — so the risk here is not
        that v2 fails to parse but that it parses *as v1*, which would put the
        two versions back in one world and make the run-start guard the only
        thing separating them.
        """
        self.assertIs(pm.parser_for(pm.SCHEMA_V2), pm.PlanV2)
        self.assertIsNot(pm.parser_for(pm.SCHEMA_V2), pm.Plan)
        data = plan_mapping()
        data["schema_version"] = pm.SCHEMA_V2
        plan = pm.parse_mapping(data)
        self.assertIsInstance(plan, pm.PlanV2)
        self.assertEqual(pm.SCHEMA_V2, plan.schema_version)

    def test_neither_class_will_wear_the_other_version_marker(self):
        """The marker is a `Literal` field, not a free string. Without this,
        `PlanV2` subclassing `Plan` would let a v1 body validate as v2 and the
        registry's dispatch would be decoration."""
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            pm.Plan.model_validate({**plan_mapping(),
                                    "schema_version": pm.SCHEMA_V2})
        with self.assertRaises(ValidationError):
            pm.PlanV2.model_validate(plan_mapping())

    def test_an_unknown_version_is_a_typed_refusal_not_a_guess(self):
        data = plan_mapping()
        data["schema_version"] = "maestro-plan.v7"
        with self.assertRaises(pm.SchemaVersionUnknown):
            pm.parse_mapping(data)

    def test_a_registered_version_cannot_be_rebound(self):
        with self.assertRaises(pm.SchemaVersionFrozen):
            pm.register_parser(pm.SCHEMA_V1, pm.Plan)

    def test_there_is_no_upgrade_function(self):
        """§6.3 — a new field means a new version string and a new class."""
        source = (ADWS / "adw_modules" / "plan_model.py").read_text()
        tree = ast.parse(source)
        names = {node.name for node in ast.walk(tree)
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        for banned in ("upgrade", "migrate", "convert", "upgrade_plan",
                       "migrate_plan"):
            self.assertNotIn(banned, names)


class ImportBoundaryTests(unittest.TestCase):
    """§6.3, §12.2 — the shipped invariant: the runtime never re-canonicalizes.

    `canonicalize` exists in exactly one module, and that module is imported
    only by authoring and validation. `digest_of` is the identity function
    everything else uses, and it lives in a module that imports no model at
    all — so a digest cannot be taken over a re-serialisation of a parsed
    model even by accident.
    """

    #: The only call sites §6.3 permits: the writer and the validator.
    CANONICAL_IMPORTERS = {
        "plan_canonical.py", "plan_validate.py", "plan_author.py",
    }

    def modules(self):
        return sorted((ADWS / "adw_modules").glob("*.py"))

    def imported_module_names(self, path: Path):
        names = set()
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                names.update(alias.name.split(".")[-1] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    names.add(node.module.split(".")[-1])
                names.update(alias.name for alias in node.names)
        return names

    def test_canonicalize_is_defined_in_exactly_one_module(self):
        definers = []
        for path in self.modules():
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef) and node.name == "canonicalize":
                    definers.append(path.name)
        self.assertEqual(definers, ["plan_canonical.py"])

    def test_no_runtime_module_imports_the_canonicalizer(self):
        offenders = []
        for path in self.modules():
            if path.name in self.CANONICAL_IMPORTERS:
                continue
            if "plan_canonical" in self.imported_module_names(path):
                offenders.append(path.name)
        self.assertEqual(offenders, [])

    def test_no_adw_script_imports_the_canonicalizer(self):
        offenders = [path.name for path in sorted(ADWS.glob("*.py"))
                     if "plan_canonical" in self.imported_module_names(path)]
        self.assertEqual(offenders, [])

    def test_the_digest_module_imports_no_plan_model(self):
        names = self.imported_module_names(ADWS / "adw_modules" / "plan_digest.py")
        for banned in ("plan_model", "plan_canonical", "plan_validate",
                       "pydantic", "json"):
            self.assertNotIn(banned, names)

    def test_digest_of_takes_bytes_and_refuses_a_parsed_model(self):
        plan = pm.parse_mapping(plan_mapping())
        with self.assertRaises(TypeError):
            pd.digest_of(plan)
        with self.assertRaises(TypeError):
            pd.digest_of(pc.canonicalize(plan).decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
