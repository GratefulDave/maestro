"""A reviewer cannot report a file it was never given.

Sealed tests live only in the vault. `_failed_run_gates` overlays them onto the
integration SHA and measures the gate itself; the integration reviewer gets a
checkout without them, by design. So a finding whose `implementation_area` names
a sealed path is not an observation -- the reviewer had nothing to observe -- and
cannot be evidence for a REVISE.

The role contract says this in words. On run a33d5e9b4a404f5889785cb1c9ca5f6f the
integration reviewer was handed that exact sentence in its own AGENTS.md --
"Sealed test paths are absent from this checkout by design, and their absence is
never a finding ... never report a missing test path, an uncollected case, a case
count, or a gate exit code" -- and reported

    "The declared pytest gate exits 4 because services/label-batch/tests/
     observations does not exist; zero cases are collected."

while the harness's own gate had just run those 15 cases and passed all 15. An
instruction is not a control, so the claim is dropped where the verdict is
recorded rather than asked about again.

These cases pin the narrowness: a finding about a file the reviewer CAN read
survives untouched, a PASS is never rewritten, and only when every finding was
unobservable does the verdict become PASS -- because a REVISE with no finding is
not a verdict.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
if str(ADWS) not in sys.path:
    sys.path.insert(0, str(ADWS))

from adw_modules import scheduler as sch  # noqa: E402
from adw_modules import scheduler_types as st  # noqa: E402

SEALED = frozenset(
    {
        "services/label-batch/tests/observations/conftest.py",
        "services/label-batch/tests/observations/test_append_only_store.py",
    }
)

UNOBSERVABLE = {
    "implementation_area": "services/label-batch/tests/observations",
    "observed_behavior": "the declared pytest gate exits 4; zero cases collected",
    "required_behavior": "the gate must collect at least 15 cases",
    "violated_requirement": "verify-wp1-observations-build gate",
}
REAL = {
    "implementation_area": "services/label-batch/label_batch/observations.py:203",
    "observed_behavior": "record declares raw_spl=None",
    "required_behavior": "record must require raw_spl",
    "violated_requirement": "claim-wp1-provenance",
}


def _scheduler(sealed: frozenset[str]) -> sch.FactoryScheduler:
    obj = sch.FactoryScheduler.__new__(sch.FactoryScheduler)
    obj._sealed_paths = lambda: sealed  # type: ignore[method-assign]
    obj._say = lambda *_a, **_k: None  # type: ignore[method-assign]
    return obj


class UnobservableFindingTests(unittest.TestCase):
    def test_a_finding_naming_a_sealed_path_is_dropped(self):
        verdict, findings, affected = _scheduler(SEALED)._drop_unobservable_findings(
            st.ReviewerVerdict.REVISE, [UNOBSERVABLE], ["lane-a"]
        )
        self.assertIs(verdict, st.ReviewerVerdict.PASS)
        self.assertEqual(findings, ())
        self.assertEqual(affected, ())

    def test_a_finding_about_a_readable_file_survives(self):
        verdict, findings, affected = _scheduler(SEALED)._drop_unobservable_findings(
            st.ReviewerVerdict.REVISE, [REAL], ["lane-a"]
        )
        self.assertIs(verdict, st.ReviewerVerdict.REVISE)
        self.assertEqual(list(findings), [REAL])
        self.assertEqual(affected, ("lane-a",))

    def test_a_real_finding_beside_an_unobservable_one_keeps_the_revise(self):
        verdict, findings, affected = _scheduler(SEALED)._drop_unobservable_findings(
            st.ReviewerVerdict.REVISE, [UNOBSERVABLE, REAL], ["lane-a"]
        )
        self.assertIs(verdict, st.ReviewerVerdict.REVISE)
        self.assertEqual(list(findings), [REAL])
        self.assertEqual(affected, ("lane-a",))

    def test_a_pass_is_never_rewritten(self):
        verdict, findings, affected = _scheduler(SEALED)._drop_unobservable_findings(
            st.ReviewerVerdict.PASS, [], []
        )
        self.assertIs(verdict, st.ReviewerVerdict.PASS)
        self.assertEqual(findings, ())
        self.assertEqual(affected, ())

    def test_a_bare_directory_name_is_not_a_sealed_path(self):
        # "tests" appears inside every sealed path as a substring. It is not one
        # of those files, and a substring match dropped a real REVISE for it.
        bare = dict(REAL, implementation_area="tests")
        verdict, findings, _ = _scheduler(SEALED)._drop_unobservable_findings(
            st.ReviewerVerdict.REVISE, [bare], ["lane-a"]
        )
        self.assertIs(verdict, st.ReviewerVerdict.REVISE)
        self.assertEqual(list(findings), [bare])

    def test_a_sealed_file_with_a_line_suffix_is_still_dropped(self):
        located = dict(
            UNOBSERVABLE,
            implementation_area=(
                "services/label-batch/tests/observations/conftest.py:12"
            ),
        )
        verdict, findings, _ = _scheduler(SEALED)._drop_unobservable_findings(
            st.ReviewerVerdict.REVISE, [located], ["lane-a"]
        )
        self.assertIs(verdict, st.ReviewerVerdict.PASS)
        self.assertEqual(findings, ())

    def test_with_no_sealed_paths_nothing_is_dropped(self):
        verdict, findings, _ = _scheduler(frozenset())._drop_unobservable_findings(
            st.ReviewerVerdict.REVISE, [UNOBSERVABLE], ["lane-a"]
        )
        self.assertIs(verdict, st.ReviewerVerdict.REVISE)
        self.assertEqual(list(findings), [UNOBSERVABLE])


if __name__ == "__main__":
    unittest.main()
