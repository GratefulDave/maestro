"""The builder is told which cases failed, not just how many.

Without this it receives a count and the reviewer's prose, so it re-guesses the
same fix every round. The lines forwarded here are the runner's own, carried on
the path `findings` already takes: redacted against the sealed token set, then
proved clean by the same `refuse_private_leak` that guards the rest of the
payload. Tracebacks are dropped rather than redacted, because a redacted
skeleton of a test is still a shape the builder must not see.
"""

import json
import pathlib
import unittest

import adw_modules.code_review as cr
import adw_modules.private_review as pr
import adw_modules.scheduler_types as st

# Taken from the real lane-wp7-build failures.
PYTEST_OUTPUT = """
============================= test session starts ==============================
collected 11 items

tests/test_paid.py FFFFFFFFFF.                                           [100%]

=================================== FAILURES ===================================
___________________ test_entity_surface_exposes_paid_panel _____________________

    def test_entity_surface_exposes_paid_panel():
        surface = paidDpa.buildEntityDpaSurface(entity)
E       TypeError: paidDpa.buildEntityDpaSurface is not a function

tests/test_paid.py:41: TypeError
=========================== short test summary info ============================
FAILED tests/test_paid.py::test_entity_surface_exposes_paid_panel
========================= 10 failed, 1 passed in 0.42s =========================
"""

VITEST_OUTPUT = """
 FAIL  src/lib/seo/paid-dpa.test.ts > buildPaidPanel > marks availability
AssertionError: expected { kind: 'ok' } to deeply equal { available: true }
 ❯ src/lib/seo/paid-dpa.test.ts:88:24
"""


class ExtractionTest(unittest.TestCase):
    def test_it_names_the_failing_symbol(self) -> None:
        lines = cr.redacted_failure_lines(PYTEST_OUTPUT, ())
        joined = "\n".join(lines)
        self.assertIn("buildEntityDpaSurface is not a function", joined)

    def test_it_keeps_the_error_class(self) -> None:
        lines = cr.redacted_failure_lines(VITEST_OUTPUT, ())
        self.assertTrue(any("AssertionError" in line for line in lines))

    def test_it_drops_the_traceback_source(self) -> None:
        # The def line and the call site are the test's own source.
        lines = cr.redacted_failure_lines(PYTEST_OUTPUT, ())
        joined = "\n".join(lines)
        self.assertNotIn("def test_entity_surface_exposes_paid_panel", joined)
        self.assertNotIn("surface = paidDpa", joined)

    def test_it_drops_session_noise(self) -> None:
        lines = cr.redacted_failure_lines(PYTEST_OUTPUT, ())
        joined = "\n".join(lines)
        self.assertNotIn("test session starts", joined)
        self.assertNotIn("collected 11 items", joined)

    def test_empty_output_yields_nothing(self) -> None:
        self.assertEqual(cr.redacted_failure_lines("", ()), ())
        self.assertEqual(cr.redacted_failure_lines("\n\n  \n", ()), ())

    def test_duplicate_lines_collapse(self) -> None:
        repeated = "\n".join(["TypeError: x is not a function"] * 12)
        self.assertEqual(len(cr.redacted_failure_lines(repeated, ())), 1)

    def test_the_line_budget_is_honoured(self) -> None:
        many = "\n".join(
            "TypeError: sym{0} is not a function".format(i) for i in range(200)
        )
        self.assertEqual(len(cr.redacted_failure_lines(many, ())), 40)
        self.assertEqual(len(cr.redacted_failure_lines(many, (), limit=5)), 5)

    def test_a_very_long_line_is_truncated(self) -> None:
        line = "AssertionError: " + ("x" * 5000)
        kept = cr.redacted_failure_lines(line, ())
        self.assertEqual(len(kept), 1)
        self.assertLessEqual(len(kept[0]), 300)


class CaseNameTest(unittest.TestCase):
    """A test's own name is private, and redaction cannot protect it.

    The token set is collected from the sealed files, so a case name it happens
    to miss would pass straight through. Every line shape that carries one is
    dropped whole instead. Caught by the real-runner contract test, which found
    `test_refund_rejects_secret_negative` surviving in a payload.
    """

    def test_the_pytest_short_summary_is_dropped(self) -> None:
        output = "FAILED tests/t.py::test_refund_rejects_secret_negative - Assert"
        self.assertEqual(cr.redacted_failure_lines(output, ()), ())

    def test_a_pytest_node_id_is_dropped_anywhere(self) -> None:
        output = "AssertionError in tests/t.py::test_secret_case_name failed"
        self.assertEqual(cr.redacted_failure_lines(output, ()), ())

    def test_the_vitest_fail_header_is_dropped(self) -> None:
        output = " FAIL  src/a.test.ts > buildPaidPanel > marks secret thing"
        self.assertEqual(cr.redacted_failure_lines(output, ()), ())

    def test_the_vitest_file_pointer_is_dropped(self) -> None:
        self.assertEqual(
            cr.redacted_failure_lines(" \u276f src/a.test.ts:88:24", ()), ()
        )

    def test_no_case_name_survives_a_full_run(self) -> None:
        for output in (PYTEST_OUTPUT, VITEST_OUTPUT):
            joined = "\n".join(cr.redacted_failure_lines(output, ()))
            self.assertNotIn("test_entity_surface_exposes_paid_panel", joined)
            self.assertNotIn("marks availability", joined)
            self.assertNotIn("::", joined)

    def test_the_defect_line_still_survives(self) -> None:
        # Dropping case names must not drop the reason the case failed.
        joined = "\n".join(cr.redacted_failure_lines(PYTEST_OUTPUT, ()))
        self.assertIn("is not a function", joined)


class RedactionTest(unittest.TestCase):
    def test_a_private_token_never_survives(self) -> None:
        tokens = ("available", "buildEntityDpaSurface")
        for output in (PYTEST_OUTPUT, VITEST_OUTPUT):
            for line in cr.redacted_failure_lines(output, tokens):
                for token in tokens:
                    self.assertNotIn(token, line, line)

    def test_redaction_leaves_the_surrounding_signal(self) -> None:
        lines = cr.redacted_failure_lines(VITEST_OUTPUT, ("available",))
        joined = "\n".join(lines)
        self.assertIn("AssertionError", joined)
        self.assertNotIn("available", joined)

    def test_the_existing_leak_guard_accepts_the_redacted_lines(self) -> None:
        # Redact over token set S, then assert nothing from S survived -- the
        # same two-step the findings path already uses.
        tokens = ("available", "buildEntityDpaSurface", "buildPaidPanel")
        payload = {
            "redacted_failures": list(
                cr.redacted_failure_lines(PYTEST_OUTPUT + VITEST_OUTPUT, tokens)
            )
        }
        pr.refuse_private_leak(payload, tokens, allow=())

    def test_an_unredacted_line_is_refused_by_that_guard(self) -> None:
        # Proves the guard above is load-bearing rather than vacuous.
        tokens = ("buildEntityDpaSurface",)
        payload = {"redacted_failures": ["TypeError: buildEntityDpaSurface"]}
        with self.assertRaises(Exception):
            pr.refuse_private_leak(payload, tokens, allow=())


class SurfaceNamesSurviveTest(unittest.TestCase):
    """Names the builder already holds are not redacted out of the signal.

    `bound_surface` sends it module specifiers, exported symbols, and object
    keys in the same prompt. Blanking those same names here produced lines like
    `{ [redacted] } to deeply equal { [redacted] }`, which tell the builder
    strictly less than the failure count it already had. Observed live on
    lane-wp7-build: 15 forwarded lines, most of them shaped exactly like that.
    """

    LINE = "\u2192 expected { kind: 'ok', available: false } to deeply equal { available: true }"

    def test_without_the_allow_list_the_shape_is_destroyed(self) -> None:
        kept = cr.redacted_failure_lines(self.LINE, ("available", "kind"))
        self.assertNotIn("available", kept[0])

    def test_with_the_allow_list_the_keys_survive(self) -> None:
        kept = cr.redacted_failure_lines(
            self.LINE, ("available", "kind"), allow=("available", "kind")
        )
        self.assertIn("available", kept[0])
        self.assertIn("kind", kept[0])

    def test_a_value_is_still_redacted_when_its_key_is_allowed(self) -> None:
        line = "\u2192 expected { token: 'hunter2' } to deeply equal { token: 'sekrit' }"
        kept = cr.redacted_failure_lines(
            line, ("token", "hunter2", "sekrit"), allow=("token",)
        )
        self.assertIn("token", kept[0])
        self.assertNotIn("hunter2", kept[0])
        self.assertNotIn("sekrit", kept[0])

    def test_an_empty_allow_list_changes_nothing(self) -> None:
        self.assertEqual(
            cr.redacted_failure_lines(self.LINE, ("available",)),
            cr.redacted_failure_lines(self.LINE, ("available",), allow=()),
        )

    def test_the_surface_deriver_returns_names_only(self) -> None:
        files = {
            "t.test.ts": (
                "import { buildPaidPanel } from '@/lib/seo/paid-dpa';\n"
                "it('x', () => { expect(buildPaidPanel(e)).toEqual("
                "{ available: true, secret: 'hunter2' }); });\n"
            )
        }
        names = cr._bound_surface_names(files)
        self.assertIn("buildPaidPanel", names)
        self.assertIn("available", names)
        self.assertNotIn("hunter2", names)
        self.assertNotIn("true", names)

    def test_the_deriver_never_raises_the_review_away(self) -> None:
        self.assertEqual(cr._bound_surface_names({"x.py": "def ("}), ())


class PytestTracebackModeTest(unittest.TestCase):
    """pytest must print assertion detail or there is nothing to forward.

    `--tb=no` emitted only the short summary, whose entire content is
    `path::case_name`. Those are private and correctly dropped, so
    lane-wp7-gateway-build forwarded zero lines on every round for eight
    rounds while appearing to work.
    """

    def test_the_sealed_pytest_invocation_asks_for_line_tracebacks(self) -> None:
        import adw_modules.tests_chain as tc

        source = pathlib.Path(tc.__file__).read_text()
        self.assertIn('"--tb=line"', source)
        self.assertNotIn('"--tb=no"', source)

    def test_a_line_traceback_survives_extraction(self) -> None:
        # The real shape pytest emits under --tb=line.
        output = (
            "/repo/tests/t.py:7: AttributeError: 'NoneType' object has no "
            "attribute 'headers'"
        )
        kept = cr.redacted_failure_lines(output, ())
        self.assertEqual(len(kept), 1)
        self.assertIn("has no attribute 'headers'", kept[0])

    def test_the_private_path_in_it_is_still_redacted(self) -> None:
        output = "/repo/tests/t.py:7: AttributeError: boom"
        kept = cr.redacted_failure_lines(output, ("/repo/tests/t.py",))
        self.assertNotIn("/repo/tests/t.py", kept[0])
        self.assertIn("AttributeError: boom", kept[0])


PYTEST_VV_DIFF = """
E   AssertionError: assert {'prr': 3.21, 'ic_lower': 0.83} == {'prrCiLow': 3.21}
E     
E     Common items:
E     {'eb05': 1.64, 'ebgm': 2.87}
E     Left contains 7 more items:
E     {'chi_sq': None,
E      'ic_lower': 0.83,
E      'prr_lower': 1.83}
E     Right contains 6 more items:
E     {'icCiHigh': 2.01,
E      'prrCiLow': 1.83}
tests/t.py:447: AssertionError
"""


class ComparisonBodyTest(unittest.TestCase):
    """The diff under a failure is the answer to a shape mismatch.

    lane-wp7-gateway-build ran fourteen rounds on `assert {'prr': 3.21,...} ==
    {'prr': 3.21,...}` -- pytest's truncation rendered both sides identically,
    so the forwarded line said a dict differed from itself. The block naming
    the keys was thrown away for matching no marker.
    """

    def test_the_diff_keys_are_forwarded(self) -> None:
        joined = "\n".join(cr.redacted_failure_lines(PYTEST_VV_DIFF, ()))
        for key in ("chi_sq", "ic_lower", "prr_lower", "icCiHigh", "prrCiLow"):
            self.assertIn(key, joined, key)

    def test_the_item_counts_survive(self) -> None:
        joined = "\n".join(cr.redacted_failure_lines(PYTEST_VV_DIFF, ()))
        self.assertIn("Left contains 7 more items", joined)
        self.assertIn("Right contains 6 more items", joined)

    def test_every_value_in_a_rendering_is_redacted(self) -> None:
        # Values are the test's expectations even when the token set never saw
        # them, so numbers inside a rendered collection go regardless.
        for line in cr.redacted_failure_lines(PYTEST_VV_DIFF, ()):
            if "{" not in line:
                continue
            for value in ("3.21", "0.83", "1.64", "2.87", "1.83", "2.01"):
                self.assertNotIn(value, line, line)

    def test_a_key_ending_in_a_digit_keeps_it(self) -> None:
        joined = "\n".join(cr.redacted_failure_lines(PYTEST_VV_DIFF, ()))
        self.assertIn("eb05", joined)

    def test_a_scalar_comparison_keeps_its_numbers(self) -> None:
        # `assert 404 == 200` is a continuation line, and its status codes are
        # contract vocabulary rather than fixture values.
        out = "E   AssertionError: boom\n    assert 404 == 200\n"
        joined = "\n".join(cr.redacted_failure_lines(out, ()))
        self.assertIn("assert 404 == 200", joined)

    def test_an_indented_blank_line_does_not_end_the_block(self) -> None:
        # pytest indents the separator inside a comparison body. Treating it as
        # a terminator dropped every diff.
        joined = "\n".join(cr.redacted_failure_lines(PYTEST_VV_DIFF, ()))
        self.assertIn("Common items", joined)

    def test_an_unindented_blank_line_does_end_the_block(self) -> None:
        out = "E   AssertionError: boom\n{'a': 1}\n\nunrelated {'b': 2} line\n"
        joined = "\n".join(cr.redacted_failure_lines(out, ()))
        self.assertNotIn("unrelated", joined)

    def test_case_names_are_still_dropped_from_a_block(self) -> None:
        joined = "\n".join(cr.redacted_failure_lines(PYTEST_VV_DIFF, ()))
        self.assertNotIn("::", joined)


# Captured verbatim from run f50638ab / lane-wp7-gateway-build, the round that
# crashed `run resume`. `-vv` is what makes pytest print the Full diff block,
# and the diff is where the key names live.
GATEWAY_VV_DIFF = """
=================================== FAILURES ===================================
E   AssertionError: assert {'prr': 3.21, 'prr_lower': 1.83} == {'prr': 3.21, 'prrCiLow': 1.83}
    Common items:
    {'prr': 3.21}
    Left contains 1 more item:
    {'prr_lower': 1.83}
    Right contains 1 more item:
    {'prrCiLow': 1.83}
    Full diff:
    {
    -     'prrCiLow': 1.83,
    +     'prr_lower': 1.83,
    }
"""


class ViewAgreesWithPayloadTest(unittest.TestCase):
    """One allowance guards the payload and the view that embeds it.

    `redacted_failures` deliberately keeps bound-surface names -- the builder
    is handed them anyway -- and the payload check exempts them. `builder_view`
    re-derived a narrower allowance from the public contract alone, so it
    refused a name the payload check had just accepted and `run resume` died
    with `PrivateLeakError` at REVIEWING_CODE. Observed live on
    lane-wp7-gateway-build over the keys prrCiLow/prrCiHigh/rorCiLow/
    rorCiHigh/icCiHigh, whose values were redacted the whole time.
    """

    CONTRACT = {
        "acceptance_criteria": ("the gateway maps disproportionality keys",),
        "declared_outputs": ("services/api-gateway/app/main.py",),
    }
    SEALED_DIGEST = "a" * 64
    TOKENS = ("prrCiLow", "prr_lower", "1.83", "3.21")

    def _prior(self, failures):
        return st.LaneArtifact(
            kind=st.ArtifactKind.CODE_REVIEW,
            plan_revision=1,
            spec_digest="b" * 64,
            lane_projection_digest="c" * 64,
            input_digest="d" * 64,
            output_digest="e" * 64,
            artifact_ref="refs/maestro/private-results/r/l/" + "f" * 64,
            payload={
                "findings": [
                    {
                        "implementation_area": "services/api-gateway/app/main.py",
                        "observed_behavior": "the mapper emits snake_case keys",
                        "required_behavior": "emit the contract's camelCase keys",
                        "violated_requirement": "the gateway maps disproportionality keys",
                    }
                ],
                "input_digest": "d" * 64,
                "public_result_summary": {"failed": 5, "passed": 7},
                "redacted_failures": list(failures),
                "verdict": st.ReviewerVerdict.REVISE.value,
            },
            verdict=st.ReviewerVerdict.REVISE,
        )

    def _view(self, failures, allow_names):
        return cr.builder_view(
            public_contract=self.CONTRACT,
            architecture_constraints=("keep the handler pure",),
            sealed_digest=self.SEALED_DIGEST,
            prior_code_review=self._prior(failures),
            private_tokens=self.TOKENS,
            allow_names=allow_names,
        )

    def test_the_surface_name_reaches_the_forwarded_lines(self) -> None:
        surface = ("prrCiLow", "prr", "prr_lower")
        kept = cr.redacted_failure_lines(GATEWAY_VV_DIFF, self.TOKENS, allow=surface)
        joined = "\n".join(kept)
        self.assertIn("prrCiLow", joined)
        self.assertIn("prr_lower", joined)
        # The names are the answer; the values are still secrets.
        self.assertNotIn("1.83", joined)

    def test_without_the_shared_allowance_the_view_refuses(self) -> None:
        surface = ("prrCiLow", "prr", "prr_lower")
        kept = cr.redacted_failure_lines(GATEWAY_VV_DIFF, self.TOKENS, allow=surface)
        with self.assertRaises(pr.PrivateLeakError):
            self._view(kept, ())

    def test_with_the_shared_allowance_the_view_is_built(self) -> None:
        surface = ("prrCiLow", "prr", "prr_lower")
        kept = cr.redacted_failure_lines(GATEWAY_VV_DIFF, self.TOKENS, allow=surface)
        view = self._view(kept, surface)
        blob = json.dumps(view)
        self.assertIn("prrCiLow", blob)
        self.assertNotIn("1.83", blob)

    def test_the_allowance_does_not_exempt_a_value(self) -> None:
        # Widening by a name must not smuggle a fixture value through with it.
        kept = ("E   AssertionError: expected 1.83",)
        with self.assertRaises(pr.PrivateLeakError):
            self._view(kept, ("prrCiLow",))

    def test_an_empty_allowance_matches_the_old_behaviour(self) -> None:
        view = self._view(("E   AssertionError: NameError",), ())
        self.assertEqual(view["sealed_digest"], self.SEALED_DIGEST)


if __name__ == "__main__":
    unittest.main()
