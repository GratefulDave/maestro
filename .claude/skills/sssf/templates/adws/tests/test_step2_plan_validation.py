"""Executable proof of §6.4 — the twelve deterministic obligations.

Every test here builds a real throwaway git repository and runs real `git`,
because eleven of the twelve obligations are computed from git objects alone
and a mocked object store would simply assert this module's own beliefs back
at it. The two things that are *not* git facts — whether a receipt exists for
the superseded digest, and how many cases a selector collects — enter through
injected protocol seams, which is what lets the obligations stay
deterministic while the tests stay real.

Each obligation gets a red test and the green control shares one fixture, so
§13.4's rule holds here too: a check that convicts nothing has not been shown
to convict anything.

  §6.4  the twelve, and that every one of them can fail on its own
  §6.4  "gate command core" is defined, so a reordered flag does not evade it
  §6.4  the same comparison runs against the integration gate
  §6.4  gate executability has two arms — produced paths are checked for
        well-formedness, everything else for a collection count
  §6.4  environment checks are not eligibility obligations
  §6.2  hypothesis quarantine — only an agent node's `reads` may hold one
  §6.3  the digest is taken over the stored bytes, and non-canonical bytes
        are refused rather than quietly rewritten into agreement
  §11.1 one outcome: ELIGIBLE prints a digest and publishes nothing;
        BLOCKED prints typed blockers with JSON pointers and has no digest

Run with:  uv run adws/adw_test.py -k plan_validation
"""

from __future__ import annotations

import ast
import copy
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

from adw_modules import plan_canonical as pc  # noqa: E402
from adw_modules import plan_digest as pd  # noqa: E402
from adw_modules import plan_model as pm  # noqa: E402
from adw_modules import runner_resolution as rr
from adw_modules import plan_validate as pv  # noqa: E402

README = "fixture repository\n"
EXISTING_TEST = """
import unittest


class T(unittest.TestCase):
    def test_one(self):
        self.assertTrue(True)

    def test_two(self):
        self.assertTrue(True)
"""


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _git(cwd: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                            text=True)
    if check and result.returncode != 0:
        raise AssertionError(
            "git {0} -> {1}: {2}".format(" ".join(args), result.returncode,
                                         result.stderr))
    return result.stdout.strip()


def make_repo(root: Path) -> Path:
    """A repository holding the evidence the reference plan cites.

    `tests/test_existing.py` is present at base, so a gate selecting it is
    checked for a collection count. `tests/test_greeting.py` is absent, so a
    gate selecting it falls to the produced arm — which is the golden
    scenario's own producer node, the case §6.4 says a literal reading of the
    obligation would make ineligible.
    """
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "harness@example.invalid")
    _git(repo, "config", "user.name", "Harness")
    _git(repo, "config", "core.hooksPath", str(root / "no-such-hooks"))
    (repo / "README.md").write_text(README)
    (repo / "tests").mkdir()
    (repo / "tests" / "test_existing.py").write_text(EXISTING_TEST)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return repo


def plan_mapping(base_commit: str) -> dict:
    """The reference plan: two agent nodes, one code node, three evidence."""
    return {
        "schema_version": "maestro-plan.v1",
        "plan_id": "plan-001",
        "repo": "example/repo",
        "base_commit": base_commit,
        "intent": "add a greeting and cover it",
        "evidence": [
            {"kind": "observed", "evidence_id": "e-readme",
             "path": "README.md", "sha256": sha256_text(README)},
            {"kind": "produced", "evidence_id": "e-greeting",
             "path": "src/greeting.py", "producer": "n-write",
             "base_sha256": None},
            {"kind": "hypothesis", "evidence_id": "e-guess",
             "statement": "the greeting belongs beside the entrypoint"},
        ],
        "nodes": [
            {"kind": "agent", "node_id": "n-write", "needs": [],
             "reads": ["e-readme", "e-guess"],
             "outputs": ["src/greeting.py", "tests/test_greeting.py"],
             "instruction": "write the greeting and its test",
             "gate": {"runner": "pytest", "cwd": ".",
                      "argv": ["tests/test_greeting.py"], "min_cases": 1},
             "prompt_assets": [{"role": "system", "path": "prompts/write.md",
                                "sha256": "b" * 64}]},
            {"kind": "agent", "node_id": "n-cover", "needs": [],
             "reads": ["e-readme"], "outputs": ["src/cover.py"],
             "instruction": "widen the existing coverage",
             "gate": {"runner": "pytest", "cwd": ".",
                      "argv": ["tests/test_existing.py"], "min_cases": 2},
             "prompt_assets": []},
            {"kind": "code", "node_id": "n-suite",
             "needs": ["n-write", "n-cover"], "reads": ["e-greeting"],
             "outputs": [], "command": ["pytest", "-q"], "cwd": ".",
             "expects_changes": False},
        ],
        "merge_policy": {
            "integration_branch": "main",
            "integration_gate": {"runner": "pytest", "cwd": ".",
                                 "argv": ["tests"], "min_cases": 1},
        },
        "supersedes": None,
    }


class Collector:
    """A gate collector seam. Real collection shells out to a runner, which
    is an environment fact rather than a git fact (§6.4)."""

    def __init__(self, counts=None):
        self.counts = {"tests/test_existing.py": 2, "tests": 3}
        if counts:
            self.counts.update(counts)
        self.calls = []

    def collect(self, gate, tree):
        selector = pm.selector_of(gate)
        self.calls.append(selector)
        return self.counts.get(selector, 0)


class Receipts:
    """The finalization store's read side, as this lane's boundary Protocol.
    Step 3 owns the real one (§6.5, §6.6)."""

    def __init__(self, digests=()):
        self.digests = set(digests)

    def has_receipt(self, digest):
        return digest in self.digests


class ValidationTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.repo = make_repo(self.root)
        self.base = _git(self.repo, "rev-parse", "HEAD")
        self.collector = Collector()
        self.receipts = Receipts()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def mapping(self) -> dict:
        return plan_mapping(self.base)

    def stored(self, mapping=None) -> bytes:
        return pc.canonicalize(pm.parse_mapping(mapping or self.mapping()))

    def validate(self, mapping=None, stored=None, **kw):
        payload = stored if stored is not None else self.stored(mapping)
        return pv.validate_plan(
            payload, self.repo,
            receipts=kw.pop("receipts", self.receipts),
            collector=kw.pop("collector", self.collector), **kw)

    def assertBlocked(self, result, obligation):
        self.assertEqual(result.outcome, pv.Outcome.AUTHORING_BLOCKED)
        found = [b for b in result.blockers if b.obligation is obligation]
        self.assertTrue(found, "expected {0}, got {1}".format(
            obligation, [b.obligation for b in result.blockers]))
        for blocker in found:
            self.assertTrue(blocker.pointer.startswith("/") or blocker.pointer == "",
                            "a blocker points into the plan with a JSON pointer")
            self.assertTrue(blocker.message.strip())
        return found


class TheGreenControl(ValidationTestCase):
    def test_the_reference_plan_is_eligible(self):
        stored = self.stored()
        result = self.validate(stored=stored)
        self.assertEqual(result.blockers, ())
        self.assertEqual(result.outcome, pv.Outcome.FINALIZATION_ELIGIBLE)
        self.assertTrue(result.eligible)
        self.assertEqual(result.digest, pd.digest_of(stored))

    def test_eligible_publishes_nothing_but_the_digest(self):
        """§11.1 — validate prints a digest; publication is finalize's."""
        result = self.validate()
        self.assertIsNotNone(result.digest)
        self.assertFalse(getattr(result, "published", False))
        self.assertEqual(list(self.root.glob("*receipt*")), [])

    def test_blocked_carries_no_digest_at_all(self):
        data = self.mapping()
        data["merge_policy"]["integration_branch"] = "no-such-branch"
        result = self.validate(data)
        self.assertEqual(result.outcome, pv.Outcome.AUTHORING_BLOCKED)
        self.assertIsNone(result.digest)

    def test_there_are_exactly_twelve_obligations(self):
        self.assertEqual(len(pv.OBLIGATIONS), 12)
        self.assertEqual(len(set(pv.OBLIGATIONS)), 12)
        self.assertEqual(set(pv.OBLIGATIONS), set(pv.Obligation))

    def test_every_obligation_is_reported_not_only_the_first(self):
        """§11.1 emits typed blockers, plural. A fail-fast validator would
        make an author fix twelve plans instead of one."""
        data = self.mapping()
        data["merge_policy"]["integration_branch"] = "no-such-branch"
        data["base_commit"] = "0" * 40
        data["nodes"][1]["outputs"] = ["src/greeting.py"]
        result = self.validate(data)
        reported = {b.obligation for b in result.blockers}
        self.assertIn(pv.Obligation.BRANCHES_EXIST, reported)
        self.assertIn(pv.Obligation.BASE_COMMIT_EXISTS, reported)
        self.assertIn(pv.Obligation.SINGLE_OUTPUT_OWNER, reported)


class ClosedParse(ValidationTestCase):
    def test_a_stray_field_blocks_with_a_pointer_to_itself(self):
        data = self.mapping()
        data["reviewed_by"] = "ada"
        stored = json.dumps(data, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        found = self.assertBlocked(self.validate(stored=stored),
                                   pv.Obligation.CLOSED_PARSE)
        self.assertTrue(any("reviewed_by" in b.pointer for b in found))

    def test_malformed_supersedes_blocks_at_schema_parse_before_receipt_access(self):
        data = self.mapping()
        data["supersedes"] = "A" * 64
        stored = json.dumps(data, sort_keys=True, separators=(",", ":")).encode() + b"\n"

        class ReceiptsMustNotBeRead:
            def has_receipt(_self, _digest):
                self.fail("a malformed supersedes digest must not reach receipt access")

        found = self.assertBlocked(
            self.validate(stored=stored, receipts=ReceiptsMustNotBeRead()),
            pv.Obligation.CLOSED_PARSE)
        self.assertEqual([blocker.pointer for blocker in found], ["/supersedes"])

    def test_non_canonical_bytes_are_refused_not_rewritten(self):
        """§6.3 — two byte-different files with one meaning would be two
        identities for one plan."""
        loose = json.dumps(self.mapping(), indent=2).encode("utf-8")
        self.assertBlocked(self.validate(stored=loose),
                           pv.Obligation.CLOSED_PARSE)

    def test_an_unknown_schema_version_blocks_rather_than_upgrading(self):
        data = self.mapping()
        data["schema_version"] = "maestro-plan.v9"
        stored = json.dumps(data, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        self.assertBlocked(self.validate(stored=stored),
                           pv.Obligation.CLOSED_PARSE)

    def test_a_parse_failure_reports_only_the_parse_obligation(self):
        """Nothing downstream of a closed parse has a model to run against."""
        result = self.validate(stored=b"{not json\n")
        self.assertEqual({b.obligation for b in result.blockers},
                         {pv.Obligation.CLOSED_PARSE})


class ReferencesResolveExactlyOnce(ValidationTestCase):
    def test_a_need_naming_no_node_blocks(self):
        data = self.mapping()
        data["nodes"][2]["needs"] = ["n-write", "n-ghost"]
        self.assertBlocked(self.validate(data),
                           pv.Obligation.REFERENCES_RESOLVE_ONCE)

    def test_a_read_naming_no_evidence_blocks(self):
        data = self.mapping()
        data["nodes"][1]["reads"] = ["e-ghost"]
        self.assertBlocked(self.validate(data),
                           pv.Obligation.REFERENCES_RESOLVE_ONCE)

    def test_two_nodes_with_one_id_resolve_twice_and_block(self):
        data = self.mapping()
        data["nodes"][1]["node_id"] = "n-write"
        self.assertBlocked(self.validate(data),
                           pv.Obligation.REFERENCES_RESOLVE_ONCE)

    def test_two_evidence_entries_with_one_id_block(self):
        data = self.mapping()
        data["evidence"][1]["evidence_id"] = "e-readme"
        self.assertBlocked(self.validate(data),
                           pv.Obligation.REFERENCES_RESOLVE_ONCE)

    def test_a_producer_naming_no_node_blocks(self):
        data = self.mapping()
        data["evidence"][1]["producer"] = "n-ghost"
        self.assertBlocked(self.validate(data),
                           pv.Obligation.REFERENCES_RESOLVE_ONCE)


class GraphIsAcyclic(ValidationTestCase):
    def test_a_two_node_cycle_blocks(self):
        data = self.mapping()
        data["nodes"][0]["needs"] = ["n-cover"]
        data["nodes"][1]["needs"] = ["n-write"]
        self.assertBlocked(self.validate(data), pv.Obligation.GRAPH_ACYCLIC)

    def test_a_cycle_does_not_hang_the_validator(self):
        data = self.mapping()
        data["nodes"][0]["needs"] = ["n-cover"]
        data["nodes"][1]["needs"] = ["n-suite"]
        result = self.validate(data)
        self.assertEqual(result.outcome, pv.Outcome.AUTHORING_BLOCKED)


class ExactlyOneOwnerPerOutputPath(ValidationTestCase):
    def test_two_nodes_owning_one_path_block(self):
        data = self.mapping()
        data["nodes"][1]["outputs"] = ["src/greeting.py"]
        found = self.assertBlocked(self.validate(data),
                                   pv.Obligation.SINGLE_OUTPUT_OWNER)
        self.assertTrue(any("greeting" in b.message for b in found))

    def test_one_node_declaring_a_path_twice_blocks(self):
        data = self.mapping()
        data["nodes"][1]["outputs"] = ["src/cover.py", "src/cover.py"]
        self.assertBlocked(self.validate(data),
                           pv.Obligation.SINGLE_OUTPUT_OWNER)


class EvidenceTypingAgainstGit(ValidationTestCase):
    def test_a_fabricated_observed_sha_fails_because_git_is_re_read(self):
        data = self.mapping()
        data["evidence"][0]["sha256"] = "f" * 64
        self.assertBlocked(self.validate(data),
                           pv.Obligation.EVIDENCE_TYPED_AGAINST_GIT)

    def test_an_observed_path_absent_at_base_blocks(self):
        data = self.mapping()
        data["evidence"][0]["path"] = "docs/never-written.md"
        self.assertBlocked(self.validate(data),
                           pv.Obligation.EVIDENCE_TYPED_AGAINST_GIT)

    def test_a_producer_that_does_not_own_the_path_blocks(self):
        data = self.mapping()
        data["evidence"][1]["producer"] = "n-cover"
        self.assertBlocked(self.validate(data),
                           pv.Obligation.EVIDENCE_TYPED_AGAINST_GIT)

    def test_a_produced_path_present_at_base_needs_its_declared_base_sha(self):
        data = self.mapping()
        data["evidence"][1]["path"] = "README.md"
        data["nodes"][0]["outputs"] = ["README.md", "tests/test_greeting.py"]
        self.assertBlocked(self.validate(data),
                           pv.Obligation.EVIDENCE_TYPED_AGAINST_GIT)

    def test_a_produced_path_present_at_base_passes_with_the_right_sha(self):
        """The green control for the arm above — the rule is 'absent at base
        *or* matches a declared base sha256', not 'absent'."""
        data = self.mapping()
        data["evidence"][1]["path"] = "README.md"
        data["evidence"][1]["base_sha256"] = sha256_text(README)
        data["nodes"][0]["outputs"] = ["README.md", "tests/test_greeting.py"]
        result = self.validate(data)
        self.assertEqual(
            [b for b in result.blockers
             if b.obligation is pv.Obligation.EVIDENCE_TYPED_AGAINST_GIT], [])

    def test_a_produced_path_present_at_base_with_the_wrong_sha_blocks(self):
        data = self.mapping()
        data["evidence"][1]["path"] = "README.md"
        data["evidence"][1]["base_sha256"] = "e" * 64
        data["nodes"][0]["outputs"] = ["README.md", "tests/test_greeting.py"]
        self.assertBlocked(self.validate(data),
                           pv.Obligation.EVIDENCE_TYPED_AGAINST_GIT)


class HypothesisQuarantine(ValidationTestCase):
    def test_a_code_node_may_not_read_a_hypothesis(self):
        data = self.mapping()
        data["nodes"][2]["reads"] = ["e-greeting", "e-guess"]
        self.assertBlocked(self.validate(data),
                           pv.Obligation.HYPOTHESIS_QUARANTINE)

    def test_an_unread_hypothesis_is_not_quarantined_anywhere(self):
        """A hypothesis nobody reads is quarantined to nothing, which is not
        the same as quarantined to an agent node's reads."""
        data = self.mapping()
        data["nodes"][0]["reads"] = ["e-readme"]
        self.assertBlocked(self.validate(data),
                           pv.Obligation.HYPOTHESIS_QUARANTINE)


class GateExecutability(ValidationTestCase):
    def test_an_agent_gate_naming_no_selector_blocks(self):
        """§6.2 — falling back to the runner's default whole-tree collection
        is not a node gate, and the collection-count arm would pass it."""
        data = self.mapping()
        data["nodes"][1]["gate"]["argv"] = ["-q"]
        self.assertBlocked(self.validate(data), pv.Obligation.GATE_EXECUTABLE)

    def test_a_selector_over_existing_paths_must_collect_min_cases(self):
        data = self.mapping()
        data["nodes"][1]["gate"]["min_cases"] = 5
        self.assertBlocked(self.validate(data), pv.Obligation.GATE_EXECUTABLE)

    def test_a_selector_over_produced_paths_is_not_collected_against(self):
        """§6.4 — a nonzero count there would mean the node had nothing to do.
        The reference plan's producer node is exactly this case."""
        self.assertNotIn("tests/test_greeting.py", self.collector.calls)
        self.validate()
        self.assertNotIn("tests/test_greeting.py", self.collector.calls)

    def test_a_produced_selector_still_has_to_be_well_formed(self):
        data = self.mapping()
        data["nodes"][0]["gate"]["argv"] = ["../../etc/passwd"]
        data["nodes"][0]["outputs"] = ["src/greeting.py", "../../etc/passwd"]
        self.assertBlocked(self.validate(data), pv.Obligation.GATE_EXECUTABLE)

    def test_the_integration_gate_may_name_the_whole_suite(self):
        """§6.2 — the whole suite has exactly one place, and this is it."""
        data = self.mapping()
        data["merge_policy"]["integration_gate"]["argv"] = ["-q"]
        result = self.validate(data)
        self.assertEqual([b for b in result.blockers
                          if b.obligation is pv.Obligation.GATE_EXECUTABLE], [])

    def test_a_missing_runner_is_an_operational_refusal_not_a_blocker(self):
        """§6.4 — environment checks are not eligibility obligations. A
        missing tool has no identity consequence, so it cannot be an
        eligibility answer at all."""
        class Missing:
            def collect(self, gate, tree):
                raise pv.CollectorUnavailable("pytest is not on PATH")

        with self.assertRaises(pv.CollectorUnavailable):
            self.validate(collector=Missing())


class BaseCommitAndBranches(ValidationTestCase):
    def test_a_base_commit_that_does_not_exist_blocks(self):
        data = self.mapping()
        data["base_commit"] = "0" * 40
        self.assertBlocked(self.validate(data),
                           pv.Obligation.BASE_COMMIT_EXISTS)

    def test_a_tree_sha_is_not_a_base_commit(self):
        data = self.mapping()
        data["base_commit"] = _git(self.repo, "rev-parse", "HEAD^{tree}")
        self.assertBlocked(self.validate(data),
                           pv.Obligation.BASE_COMMIT_EXISTS)

    def test_an_integration_branch_that_does_not_exist_blocks(self):
        data = self.mapping()
        data["merge_policy"]["integration_branch"] = "no-such-branch"
        self.assertBlocked(self.validate(data), pv.Obligation.BRANCHES_EXIST)


class ReviewPayloadBudget(ValidationTestCase):
    def test_an_oversized_plan_is_refused_not_chunked(self):
        """§6.5 — plan-scoped checks are whole-graph judgments; a chunked
        reviewer skips or fabricates them."""
        config = pv.ValidationConfig(review_payload_budget_bytes=64)
        self.assertBlocked(self.validate(config=config),
                           pv.Obligation.REVIEW_PAYLOAD_BUDGET)

    def test_the_budget_is_measured_over_the_stored_bytes(self):
        stored = self.stored()
        config = pv.ValidationConfig(review_payload_budget_bytes=len(stored))
        result = self.validate(stored=stored, config=config)
        self.assertEqual([b for b in result.blockers
                          if b.obligation is pv.Obligation.REVIEW_PAYLOAD_BUDGET],
                         [])


class LineageResolvesToAReceipt(ValidationTestCase):
    def test_a_supersedes_with_no_receipt_blocks(self):
        data = self.mapping()
        data["supersedes"] = "a" * 64
        self.assertBlocked(self.validate(data), pv.Obligation.LINEAGE_RESOLVES)

    def test_a_supersedes_with_a_receipt_is_eligible(self):
        data = self.mapping()
        data["supersedes"] = "a" * 64
        result = self.validate(data, receipts=Receipts({"a" * 64}))
        self.assertEqual(result.outcome, pv.Outcome.FINALIZATION_ELIGIBLE)

    def test_no_lineage_is_not_a_missing_receipt(self):
        result = self.validate()
        self.assertEqual([b for b in result.blockers
                          if b.obligation is pv.Obligation.LINEAGE_RESOLVES], [])


class GateCoreIsNotShared(ValidationTestCase):
    def test_two_agent_nodes_may_not_share_a_gate_command_core(self):
        data = self.mapping()
        data["nodes"][1]["gate"]["argv"] = ["tests/test_greeting.py"]
        data["nodes"][1]["gate"]["min_cases"] = 1
        self.assertBlocked(self.validate(data),
                           pv.Obligation.GATE_CORE_UNSHARED)

    def test_a_reordered_flag_does_not_evade_the_obligation(self):
        """§6.4 — undefined, the twelfth obligation is evaded by a flag."""
        data = self.mapping()
        data["nodes"][0]["gate"]["argv"] = ["-q", "tests/test_greeting.py",
                                            "--tb=short"]
        data["nodes"][1]["gate"]["argv"] = ["--tb=short", "-q",
                                            "tests/test_greeting.py"]
        data["nodes"][1]["gate"]["min_cases"] = 1
        self.assertBlocked(self.validate(data),
                           pv.Obligation.GATE_CORE_UNSHARED)

    def test_an_agent_node_may_not_claim_the_integration_gates_core(self):
        """§6.4 — that node has declared the repository's suite as its own
        acceptance, which §7.4 shows cannot pass while a sibling is unmerged.
        Refused at validation rather than deadlocked at run time."""
        data = self.mapping()
        data["nodes"][1]["gate"]["argv"] = ["tests"]
        data["nodes"][1]["gate"]["min_cases"] = 1
        self.assertBlocked(self.validate(data),
                           pv.Obligation.GATE_CORE_UNSHARED)

    def test_distinct_selectors_do_not_collide(self):
        result = self.validate()
        self.assertEqual([b for b in result.blockers
                          if b.obligation is pv.Obligation.GATE_CORE_UNSHARED],
                         [])


class TheRealCollector(ValidationTestCase):
    """The seam has one production implementation, and a seam whose only
    implementation is the test's own fake is the dead-seam pattern §12.3
    prohibits. These cover what the fake cannot: the argv actually sent to a
    runner, the count read back, and the operational refusal."""

    def gate(self, argv, cwd="."):
        return pm.Gate(runner="pytest", cwd=cwd, argv=tuple(argv), min_cases=1)

    def resolved(self, executable="/usr/bin/pytest", runner="pytest"):
        """A runner already resolved, so the argv assertions below do not pay
        a capability probe to state what the flags are."""
        return rr.ResolvedRunner(runner=runner, executable=executable,
                                 origin="declared", probe_exit=5,
                                 version="stub")

    def real_collector(self, resolved=None):
        chosen = resolved if resolved is not None else self.resolved()
        return pv.SubprocessCollector(resolver=lambda *a, **k: chosen)

    def test_collection_never_executes_the_suite(self):
        argv = self.real_collector().argv_for(self.gate(["tests"]), self.repo)
        self.assertEqual(
            argv,
            ("/usr/bin/pytest", "--collect-only", "-q", "-o", "addopts=",
             "tests"))

    def test_the_collection_binary_is_resolved_and_not_the_bare_literal(self):
        """The defect this replaced: `COLLECT_ARGV["pytest"]` began with the
        string `"pytest"`, so the interpreter that enumerated a gate was
        whatever `PATH` exposed. `argv[0]` is now the resolved executable."""
        argv = self.real_collector(
            self.resolved("/repo/.venv/bin/pytest")).argv_for(
                self.gate(["tests"]), self.repo)
        self.assertEqual(argv[0], "/repo/.venv/bin/pytest")
        self.assertNotIn("pytest", argv[1:])

    def test_the_vitest_argv_lists_rather_than_watches(self):
        gate = pm.Gate(runner="vitest", cwd="web", argv=("src/a.test.ts",),
                       min_cases=1)
        argv = self.real_collector(
            self.resolved("/repo/node_modules/.bin/vitest",
                          runner="vitest")).argv_for(gate, self.repo)
        self.assertEqual(
            argv,
            ("/repo/node_modules/.bin/vitest", "list", "--run",
             "src/a.test.ts"))

    def test_an_unresolvable_runner_is_an_operational_refusal(self):
        """A runner that cannot be resolved is `CollectorUnavailable` — an
        operational refusal with no identity consequence (§6.4) — and never a
        blocker, which would say the authored bytes were wrong."""
        def refuse(runner, repo, cwd, **kwargs):
            raise rr.RunnerUnusable(runner, rr.Reason.UNRESOLVED, cwd)

        collector = pv.SubprocessCollector(resolver=refuse)
        with self.assertRaises(pv.CollectorUnavailable) as caught:
            collector.collect(self.gate(["tests"]), self.repo)
        self.assertIn(rr.RUNNER_UNUSABLE, str(caught.exception))

    def test_the_count_reads_identifiers_and_ignores_the_summary(self):
        stdout = ("tests/test_a.py::T::test_one\n"
                  "tests/test_a.py::T::test_two\n"
                  "\n"
                  "2 tests collected in 0.01s\n")
        self.assertEqual(pv.SubprocessCollector._count(stdout), 2)

    def test_a_working_directory_that_does_not_exist_is_unavailable(self):
        with self.assertRaises(pv.CollectorUnavailable):
            self.real_collector().collect(self.gate(["tests"], cwd="no-such"),
                                     self.repo)


class ValidationLaunchesNoReviewer(unittest.TestCase):
    """§11.1 — AUTHORING_BLOCKED launches no reviewer and publishes nothing,
    and §6.5's ordering invariant is that the deterministic obligations run
    before any reviewer exists. Both are properties of what this module can
    reach, so they are checked by parsing it."""

    def test_the_validator_imports_no_agent_launch_path(self):
        source = (ADWS / "adw_modules" / "plan_validate.py").read_text()
        imported = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[-1])
            elif isinstance(node, ast.Import):
                imported.update(a.name.split(".")[-1] for a in node.names)
        for banned in ("agents", "agent_cc", "agent_pi", "runner", "session",
                       "prompts"):
            self.assertNotIn(banned, imported)


if __name__ == "__main__":
    unittest.main()
