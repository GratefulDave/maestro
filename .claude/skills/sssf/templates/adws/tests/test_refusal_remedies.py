"""Every typed refusal carries its remedy — declared, enforced, rendered.

A refusal is two halves: a typed observation of what was measured wrong, and
a statement of what would satisfy the measure. The first half was always
typed; the second used to be whatever the detail prose happened to imply, and
twice on 2026-08-27 it implied nothing. `lane-routing-chemical-tests`
(run-d3bd665ce838456f989a15143f196710) received byte-identical
`TEST_STRENGTH_CONTROL_WRONG_REASON` refusals three times and spent its turns
grepping the harness for the gate implementation instead of writing cases;
`lane-wp6-tests` (run-8a200af7f9044ce7a11a51b6908f37e3) received
`TESTS_NO_NEW_CASES` twice, byte-identical, pointing at nothing it could
change. The first fix was `_remediation_lines`, which string-sniffed one
verdict of 24 out of the rendered reason — an instance patch. This suite is
the contract for the class:

* **declaration** — every member of every refusal vocabulary carries a
  non-empty remedy, supplied where the code is defined, and a member defined
  without one is a TypeError at class creation: a build failure, not a
  lint note (B15 one level up — a verdict with no remedy is a field with no
  reader on the only side that can act);
* **construction** — the helpers every raise site funnels through stamp the
  member's remedy onto the verdict;
* **reach** (B15) — the remedy survives the durable failure detail, the
  attempt-row guidance extra, ledger reconstruction, and lands in the
  rendered retry prompt beside the verdict it repairs.

The remedy is deterministic text keyed on the typed code — never a model's
opinion — and nothing transitions on it (§1.2).
"""

from __future__ import annotations

import enum
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

from adw_modules import retry_policy as rp  # noqa: E402
from adw_modules import scheduler as sch  # noqa: E402
from adw_modules import scheduler_types as st  # noqa: E402
from adw_modules import tests_chain as tc  # noqa: E402


#: Every typed refusal vocabulary a verification surface can stamp. A new
#: vocabulary belongs in this tuple; the module-scan test below is what makes
#: forgetting it a failure rather than a silent exemption.
REFUSAL_VOCABULARIES = (tc.TestsRefusal, tc.StrengthRefusal, tc.PairingRefusal)


class _Node:
    """Just enough of a PlanNode for rendering: declared outputs."""

    def __init__(self, outputs=("tests/test_x.py",)):
        self.node_id = "lane-x"
        self.outputs = tuple(outputs)


class RemedyDeclarationTests(unittest.TestCase):
    """The obligation itself: no refusal code without a remedy."""

    def test_every_refusal_code_carries_a_substantive_remedy(self):
        for vocabulary in REFUSAL_VOCABULARIES:
            for member in vocabulary:
                with self.subTest(code=member.value):
                    self.assertIsInstance(member.remedy, str)
                    # Substantive, not a placeholder: long enough to state an
                    # action, and not the code restated.
                    self.assertGreaterEqual(len(member.remedy.strip()), 80)
                    self.assertNotEqual(member.remedy.strip(), member.value)

    def test_no_refusal_vocabulary_is_exempt(self):
        """Every remedied-refusal enum in `tests_chain` is under the tests above.

        A vocabulary added later and left out of REFUSAL_VOCABULARIES would
        re-open the hole member by member; scanning the module closes it.
        """
        found = {
            obj
            for obj in vars(tc).values()
            if isinstance(obj, type)
            and issubclass(obj, tc._RemediedRefusal)
            and obj is not tc._RemediedRefusal
        }
        self.assertEqual(found, set(REFUSAL_VOCABULARIES))

    def test_a_code_defined_without_a_remedy_does_not_build(self):
        """The enforcement is the constructor, not review discipline.

        `_RemediedRefusal.__new__` has no default for `remedy`, so a member
        declared as a bare string raises at class-creation time — the build
        failure B15 demands, proven here by attempting the build.
        """
        with self.assertRaises(TypeError):

            class _Broken(tc._RemediedRefusal):  # noqa: F841
                NEW_CODE = "TESTS_NEW_CODE_WITHOUT_REMEDY"

    def test_the_wrong_reason_remedy_names_the_mechanism(self):
        """The content of the deleted instance patch survives as declaration.

        `_remediation_lines` knew the one real way out of an import-crash
        CONTROL_WRONG_REASON; that knowledge now lives on the member. It must
        name the mechanism (the crash pre-empts the assertion), the fix (catch
        inside the body, assert on the result), and the constraint that keeps
        the fix red (module-scope imports make the file uncollectable).
        """
        remedy = tc.StrengthRefusal.CONTROL_WRONG_REASON.remedy
        self.assertIn("raises before any assertion runs", remedy)
        self.assertIn("except ModuleNotFoundError", remedy)
        self.assertIn("inside the test body", remedy)
        self.assertIn("uncollectable", remedy)


class RemedyConstructionTests(unittest.TestCase):
    """Every refusal constructor stamps the declared remedy on the verdict."""

    def test_tests_refusals_carry_their_member_remedy(self):
        verdict = tc._refused(
            tc.TestsRefusal.NO_NEW_CASES, "no new collected case"
        )
        self.assertEqual(verdict.refusal_code, tc.TestsRefusal.NO_NEW_CASES.value)
        self.assertEqual(verdict.remedy, tc.TestsRefusal.NO_NEW_CASES.remedy)

    def test_strength_refusals_resolve_the_durable_string_to_its_remedy(self):
        for member in tc.StrengthRefusal:
            with self.subTest(code=member.value):
                verdict = tc._strength_refused(member.value, "detail")
                self.assertEqual(verdict.refusal_code, member.value)
                self.assertEqual(verdict.remedy, member.remedy)

    def test_an_unknown_strength_code_fails_loudly_not_remedyless(self):
        with self.assertRaises(ValueError):
            tc._strength_refused("TEST_STRENGTH_NOT_A_CODE", "detail")

    def test_pairing_refusals_default_to_the_member_remedy(self):
        verdict = sch.Scheduler._pairing_refused(
            tc.PairingRefusal.BYTES_SUBSTITUTED, "files differ"
        )
        self.assertEqual(
            verdict.refusal_code, tc.PairingRefusal.BYTES_SUBSTITUTED.value
        )
        self.assertEqual(
            verdict.remedy, tc.PairingRefusal.BYTES_SUBSTITUTED.remedy
        )

    def test_a_pairing_site_override_is_carried_verbatim(self):
        """GATE_NOT_GREEN covers three measured sub-causes; the site's more
        specific remedy wins, and it is still declared text, not inference."""
        verdict = sch.Scheduler._pairing_refused(
            tc.PairingRefusal.GATE_NOT_GREEN,
            "min_cases unsatisfiable",
            retry_class=st.RetryClass.ENVIRONMENTAL,
            remedy="Re-ship the plan with min_cases at or below 2.",
        )
        self.assertEqual(
            verdict.remedy, "Re-ship the plan with min_cases at or below 2."
        )

    def test_adjudicators_stamp_remedies_end_to_end(self):
        """The real adjudicators — not only the helpers — produce remedied
        verdicts, for a sample refusal from each measuring function."""
        red = tc.adjudicate_parent_red(
            SimpleNamespace(counts={}), new_case_count=0
        )
        self.assertEqual(red.remedy, tc.TestsRefusal.NO_NEW_CASES.remedy)
        uncontracted = tc.verify_test_strength(
            tc.GateStrengthEvidence(
                tests_node_id="lane-x-tests", candidate_sha="a" * 40,
                runner="pytest", selector="", contract_declared=False,
                gate_min_cases=tc.UNSTATED_GATE_FLOOR)
        )
        self.assertEqual(
            uncontracted.remedy, tc.StrengthRefusal.CONTRACT_ABSENT.remedy
        )


class RemedyReachTests(unittest.TestCase):
    """B15: the remedy is read where the refused agent looks — the prompt."""

    @staticmethod
    def _detail(verdict):
        classification = SimpleNamespace(reason=None, launcher_failure=None)
        return sch._failure_detail(classification, verdict)

    def test_the_remedy_survives_the_durable_failure_detail(self):
        verdict = tc._refused(tc.TestsRefusal.NO_NEW_CASES, "none")
        detail = self._detail(verdict)
        self.assertEqual(detail["remedy"], verdict.remedy)
        guidance = rp.verification_guidance(detail)
        self.assertEqual(guidance.remedy, verdict.remedy)

    def test_the_remedy_survives_the_attempt_extra_round_trip(self):
        """The durable half: extra payload out, ledger reconstruction back."""
        verdict = tc._strength_refused(
            tc.StrengthRefusal.CONTROL_NOT_RED.value, "a case survived"
        )
        extra = rp.guidance_extra_verification(self._detail(verdict))
        attempt = SimpleNamespace(
            node_id="lane-x",
            attempt_no=1,
            extra=extra,
            guidance_key=("lane-x", "base-sha"),
        )
        ledgers = rp.guidance_from_attempts([attempt])
        (entry,) = ledgers[("lane-x", "base-sha")].verification
        self.assertEqual(entry.remedy, verdict.remedy)

    def test_the_remedy_reaches_the_rendered_prompt(self):
        verdict = tc._strength_refused(
            tc.StrengthRefusal.CONTROL_WRONG_REASON.value,
            "every selected case failed, but none for the declared reason "
            "'AssertionError|feeds_mart'; observed: ModuleNotFoundError",
        )
        ledger = rp.GuidanceLedger().with_verification(
            rp.verification_guidance(self._detail(verdict))
        )
        rendered = rp.render_guidance(_Node(), ledger)
        self.assertIn("To satisfy this check:", rendered)
        # The verdict and its remedy render together: the observation ...
        self.assertIn("TEST_STRENGTH_CONTROL_WRONG_REASON", rendered)
        # ... and the mechanism-naming way out, not merely the verdict again.
        self.assertIn("except ModuleNotFoundError", rendered)
        self.assertIn("uncollectable", rendered)

    def test_a_verdict_without_a_typed_code_renders_without_the_header(self):
        """An untyped refusal (a bare red gate) has no remedy to promise."""
        ledger = rp.GuidanceLedger().with_verification(
            rp.VerificationGuidance(reason="gate exited 1", failed_clause=3)
        )
        rendered = rp.render_guidance(_Node(), ledger)
        self.assertNotIn("To satisfy this check:", rendered)

    def test_the_instance_patch_is_gone(self):
        """The string-sniffing renderer must not survive beside the class fix:
        two sources of remedy text for one verdict is the drift §4 convicts."""
        self.assertFalse(hasattr(rp, "_remediation_lines"))


if __name__ == "__main__":
    unittest.main()
