"""A stall is measured on the quantities a round can actually move.

FDAdb run d246ae95, `lane-faq-producer-tests`: the tester answered its reviewer
three times, the findings went 7, 5, 4, and the collected case count stayed 12
throughout -- because the work was rewriting assertions inside the same twelve
cases. `_stalled`'s plateau rule read 12, 12, 12 and parked the lane
`WAITING_FOR_USER` as `NO_PROGRESS` while it was converging. `lane_gates.py`
printed `stalled False` about the same lane at the same moment, because it read
CODE_REVIEW history for a lane whose argument was its tests.

So: a tests round's outcome is the pair (collected, -findings). A plateau
needs both halves flat, a falling case count stalls at any setting, and one
worse findings round stalls only where the deployment says
`stall.regression_on_findings: true`. Build rounds keep the sealed suite's
passed count, and a regression there parks at any setting.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path


import maestro
from adw_modules import scheduler as sch
from adw_modules import scheduler_types as st

TEST_REVIEW = st.ArtifactKind.TEST_REVIEW
CODE_REVIEW = st.ArtifactKind.CODE_REVIEW


def _history(outcomes, *, contents=None):
    """One (content digest, outcome) row per round, contents distinct."""
    if contents is None:
        contents = ["round-{0}".format(index) for index in range(len(outcomes))]
    return list(zip(contents, outcomes))


def _tests(rounds, *, contents=None):
    """A tests history: one (collected, findings) pair per round."""
    return _history(
        [(collected, -findings) for collected, findings in rounds],
        contents=contents,
    )


class TestsRoundsAreMeasuredByBothQuantities(unittest.TestCase):
    def test_declining_findings_over_a_flat_case_count_is_progress(self) -> None:
        # The run d246ae95 lane, exactly: collected 12, 12, 12; findings 7, 5, 4.
        history = _tests([(12, 7), (12, 5), (12, 4)])
        self.assertFalse(sch._stalled(history, TEST_REVIEW))
        self.assertFalse(
            sch._stalled(history, TEST_REVIEW, regression_on_findings=True)
        )

    def test_rising_case_count_under_a_repeated_refusal_is_progress(self) -> None:
        # A harness refusal carries one substituted finding every round, so
        # findings alone would read a draft climbing toward min_cases as flat.
        history = _tests([(6, 1), (10, 1), (14, 1)])
        self.assertFalse(sch._stalled(history, TEST_REVIEW))
        self.assertFalse(
            sch._stalled(history, TEST_REVIEW, regression_on_findings=True)
        )

    def test_a_flat_pair_plateaus_at_the_grace_window(self) -> None:
        history = _tests([(12, 4), (12, 4), (12, 4)])
        self.assertEqual(len(history), st.NO_PROGRESS_GRACE_ROUNDS)
        self.assertTrue(sch._stalled(history, TEST_REVIEW))
        self.assertTrue(
            sch._stalled(history, TEST_REVIEW, regression_on_findings=True)
        )

    def test_two_flat_rounds_are_inside_the_window(self) -> None:
        self.assertFalse(sch._stalled(_tests([(12, 4), (12, 4)]), TEST_REVIEW))

    def test_a_falling_case_count_stalls_at_either_setting(self) -> None:
        history = _tests([(12, 4), (14, 3), (11, 2)])
        self.assertTrue(sch._stalled(history, TEST_REVIEW))
        self.assertTrue(
            sch._stalled(history, TEST_REVIEW, regression_on_findings=True)
        )

    def test_one_worse_findings_round_parks_only_where_opted_in(self) -> None:
        history = _tests([(12, 3), (12, 4), (12, 5)])
        self.assertFalse(sch._stalled(history, TEST_REVIEW))
        self.assertTrue(
            sch._stalled(history, TEST_REVIEW, regression_on_findings=True)
        )

    def test_repeated_content_parks_at_either_setting(self) -> None:
        history = _tests([(12, 7), (12, 5), (12, 4)], contents=["a", "b", "a"])
        self.assertTrue(sch._stalled(history, TEST_REVIEW))
        self.assertTrue(
            sch._stalled(history, TEST_REVIEW, regression_on_findings=True)
        )

    def test_an_unmeasured_case_count_leaves_findings_readable(self) -> None:
        # Collection can fail before any case is counted. The pair still
        # plateaus, and the absent half never fabricates a regression.
        history = _tests([(None, 4), (None, 4), (None, 4)])
        self.assertTrue(sch._stalled(history, TEST_REVIEW))
        self.assertFalse(
            sch._stalled(
                _tests([(None, 4), (None, 3), (None, 2)]), TEST_REVIEW
            )
        )

    def test_a_pass_round_is_zero_findings(self) -> None:
        self.assertFalse(sch._stalled(_tests([(12, 4), (12, 2), (12, 0)]), TEST_REVIEW))


class BuildRoundsAreUnchanged(unittest.TestCase):
    def test_a_passed_count_regression_parks_without_the_key(self) -> None:
        history = _history([4, 6, 8, 6])
        self.assertTrue(sch._stalled(history, CODE_REVIEW))

    def test_the_key_does_not_reach_build_rounds(self) -> None:
        history = _history([4, 6, 8, 6])
        self.assertTrue(
            sch._stalled(history, CODE_REVIEW, regression_on_findings=False)
        )

    def test_a_rising_passed_count_continues(self) -> None:
        self.assertFalse(sch._stalled(_history([4, 6, 8]), CODE_REVIEW))

    def test_a_flat_passed_count_plateaus(self) -> None:
        self.assertTrue(sch._stalled(_history([4, 4, 4]), CODE_REVIEW))

    def test_an_empty_history_is_not_a_stall(self) -> None:
        for kind in (TEST_REVIEW, CODE_REVIEW):
            self.assertFalse(sch._stalled([], kind))


class TheKeyIsADeploymentKey(unittest.TestCase):
    def _load(self, extra: str) -> dict:
        template = Path(maestro.__file__).with_name("maestro.config.yaml").read_text(
            encoding="utf-8"
        )
        lines = [
            line
            for line in template.splitlines()
            if not line.startswith(("runtime_state_root:", "#"))
        ]
        if extra:
            lines = [line for line in lines if line != "stall:"]
            lines = [
                line
                for line in lines
                if line.strip() != "regression_on_findings: false"
            ]
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            config = repo / "adws" / "maestro.config.yaml"
            config.parent.mkdir()
            config.write_text(
                "\n".join(lines)
                + "\nruntime_state_root: {}\n{}".format(repo / "state", extra),
                encoding="utf-8",
            )
            return maestro._load_maestro_config(repo, config)

    def test_the_template_ships_it_off(self) -> None:
        template = Path(maestro.__file__).with_name("maestro.config.yaml").read_text(
            encoding="utf-8"
        )
        self.assertIn("\nstall:\n  regression_on_findings: false\n", template)
        self.assertFalse(self._load("")["stall_regression_on_findings"])

    def test_absent_means_false(self) -> None:
        self.assertFalse(
            self._load("dashboard:\n  enabled: false\n")[
                "stall_regression_on_findings"
            ]
        )

    def test_a_deployment_opts_in(self) -> None:
        loaded = self._load("stall:\n  regression_on_findings: true\n")
        self.assertTrue(loaded["stall_regression_on_findings"])

    def test_a_non_boolean_refuses(self) -> None:
        for bad in (
            "stall:\n  regression_on_findings: 'true'\n",
            "stall:\n  regression_on_findings: 1\n",
            "stall: true\n",
        ):
            with self.subTest(bad=bad), self.assertRaises(
                maestro._MaestroConfigurationError
            ):
                self._load(bad)

    def test_the_reader_is_one_function(self) -> None:
        self.assertFalse(st.stall_regression_on_findings(None))
        self.assertFalse(st.stall_regression_on_findings({}))
        self.assertFalse(st.stall_regression_on_findings({"stall": {}}))
        self.assertTrue(
            st.stall_regression_on_findings(
                {"stall": {"regression_on_findings": True}}
            )
        )
        with self.assertRaises(ValueError):
            st.stall_regression_on_findings(
                {"stall": {"regression_on_findings": "yes"}}
            )


class TheToolAndTheSchedulerAgree(unittest.TestCase):
    """`lane_gates.py` answers `stalled` with the scheduler's own function."""

    def setUp(self) -> None:
        import importlib.util

        root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location(
            "lane_gates_parity", root / "tools" / "lane_gates.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.tool = module

    def test_same_history_same_answer(self) -> None:
        cases = [
            (_tests([(12, 7), (12, 5), (12, 4)]), TEST_REVIEW),
            (_tests([(12, 4), (12, 4), (12, 4)]), TEST_REVIEW),
            (_tests([(12, 3), (12, 4), (12, 5)]), TEST_REVIEW),
            (_tests([(6, 1), (10, 1), (14, 1)]), TEST_REVIEW),
            (_history([4, 6, 8, 6]), CODE_REVIEW),
            (_history([4, 4, 4]), CODE_REVIEW),
        ]
        for history, kind in cases:
            for flag in (False, True):
                with self.subTest(kind=kind, flag=flag):
                    self.assertEqual(
                        self.tool._stalled(
                            history, kind, regression_on_findings=flag
                        ),
                        sch._stalled(history, kind, regression_on_findings=flag),
                    )

    def test_a_parked_tests_lane_reads_as_a_tests_argument(self) -> None:
        # The lane the tool disagreed about: parked, so its stage is
        # WAITING_FOR_USER, and only the wait artifact says which half stopped.
        self.assertIs(
            self.tool.review_kind_for(
                st.LaneStage.WAITING_FOR_USER.value,
                {"resume_stage": st.LaneStage.WRITING_TESTS.value},
            ),
            TEST_REVIEW,
        )
        self.assertIs(
            self.tool.review_kind_for(
                st.LaneStage.WAITING_FOR_USER.value,
                {"resume_stage": st.LaneStage.BUILDING.value},
            ),
            CODE_REVIEW,
        )
        self.assertIs(
            self.tool.review_kind_for(st.LaneStage.WRITING_TESTS.value, {}),
            TEST_REVIEW,
        )
        self.assertIs(
            self.tool.review_kind_for(st.LaneStage.BUILDING.value, {}),
            CODE_REVIEW,
        )

    def test_the_tool_reads_the_key_off_the_deployment_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            config = repo / "maestro.config.yaml"
            self.assertFalse(self.tool.stall_regression_on_findings(str(repo)))
            config.write_text("stall:\n  regression_on_findings: true\n")
            self.assertTrue(self.tool.stall_regression_on_findings(str(repo)))
            config.write_text("stall:\n  regression_on_findings: false\n")
            self.assertFalse(self.tool.stall_regression_on_findings(str(repo)))
            config.write_text("stall: not-a-mapping\n")
            self.assertFalse(self.tool.stall_regression_on_findings(str(repo)))

    def test_an_unreadable_history_reports_instead_of_raising(self) -> None:
        # A tests history needs the vault; a report never raises over a missing
        # one, it says the row is unreadable and prints the rest of the table.
        rows = dict(
            self.tool.lane_table(
                _EmptyConn(),
                "run-none",
                {
                    "lane_id": "lane-x",
                    "stage": st.LaneStage.WRITING_TESTS.value,
                    "updated_at": "now",
                },
                "/nonexistent/state-root",
                "LEVEL compared=0",
                False,
            )
        )
        self.assertEqual(rows["review_kind"], TEST_REVIEW.value)
        self.assertIn(rows["stalled"], ("False", "UNREADABLE:FileNotFoundError"))


class _EmptyCursor:
    def fetchone(self):
        return None

    def __iter__(self):
        return iter(())


class _EmptyConn:
    """A ledger with no rows: every read answers empty, nothing is written."""

    def execute(self, *_args, **_kwargs) -> _EmptyCursor:
        return _EmptyCursor()


if __name__ == "__main__":
    unittest.main()
