"""The builder is handed a way to measure itself, and told what to trust.

Two halves of one defect. The builder's checkout holds every test file in the
repository except the sealed one, so it writes code, submits, and learns three
minutes later that it was wrong -- from a count, a redacted failure line, and
another model's prose about what that line means. That prose is not
constrained to agree with the assertion it describes, and twice in one session
it was the exact inverse: a finding told the builder to preserve the very
envelope keys a sealed case forbids, and the builder followed it and regressed
a lane that had been green.

So:

  * `build()` puts a probe command in the builder's payload. It runs the
    sealed suite against the builder's own working tree and prints the same
    counts and redacted failure lines a review round would, in seconds rather
    than minutes, as often as the builder wants, before it submits.
  * `builder_view` says, above the findings, which of the two is ground truth.

These cases pin the payload's shape and the framing's presence. They do not
run the probe -- the entrypoint is a separate module with its own cases.
"""

from __future__ import annotations

import hashlib
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

import maestro  # noqa: E402
from adw_modules import code_review as cr  # noqa: E402
from adw_modules import private_review as pr  # noqa: E402
from adw_modules import scheduler_types as st  # noqa: E402


STATE_ROOT = Path("/state")
CHECKOUT = STATE_ROOT / "worktrees" / "run1" / "lane-a" / "builder" / "checkout"


def _digest(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def _actor():
    actor = maestro.HerdrStageActor.__new__(maestro.HerdrStageActor)
    actor.lane_specs = {}
    actor._roles = {}
    actor.state_root = STATE_ROOT
    actor.worktrees = STATE_ROOT / "worktrees"
    actor._base_sha = lambda ctx, role, *rest: "1" * 40
    actor._prepare = lambda ctx, role, *, sha, private_tree: (
        CHECKOUT.parent,
        CHECKOUT,
    )
    actor._bind_checkout = lambda key, attempt, checkout, used: used
    actor._commit_declared = lambda cwd, outputs, sha: ("2" * 40, True)
    actor._refresh_builder_checkout = lambda *a, **k: None
    return actor


def _ctx(artifacts=None):
    return SimpleNamespace(
        lane=SimpleNamespace(
            lane_id="lane-a",
            lane_kind=st.LANE_KIND_BUILD,
            declared_outputs=("src/lib/paidDpa.ts",),
        ),
        plan_revision=1,
        run_id="run1",
        stage=st.LaneStage.BUILDING,
        public_contract={"acceptance_criteria": []},
        sealed_digest="3" * 64,
        artifacts=artifacts if artifacts is not None else {},
        bound_surface=None,
    )


def _build_extra(artifacts=None):
    """Everything `build()` hands the launcher for the builder role."""
    actor = _actor()
    seen = {}

    def launch(ctx, role, cwd, extra, *, prepare_cwd):
        seen["extra"] = dict(extra)
        return {}, None, CHECKOUT

    actor._launch = launch
    actor.build(_ctx(artifacts))
    return seen["extra"]


class TheBuilderPayloadCarriesTheProbe(unittest.TestCase):
    """`build()` is where the probe enters, and it enters as an argv."""

    def _extra(self, artifacts=None):
        return _build_extra(artifacts)

    def test_the_argv_has_exactly_the_shape_the_probe_parses(self):
        argv = self._extra()["sealed_probe_command"]

        self.assertEqual(
            argv,
            [
                "uv",
                "run",
                str(ADWS / "sealed_probe.py"),
                "--run",
                "run1",
                "--lane",
                "lane-a",
                "--checkout",
                str(CHECKOUT),
            ],
        )

    def test_the_probe_is_the_stamped_copy_the_scheduler_itself_runs_from(self):
        # A probe resolved against the target repository, or against a second
        # adws copy, would measure a different runtime than the one that will
        # judge the candidate.
        argv = self._extra()["sealed_probe_command"]

        probe = Path(argv[2])
        self.assertEqual(probe.parent, maestro._executing_maestro_file().resolve().parent)
        self.assertEqual(probe.name, "sealed_probe.py")

    def test_the_argv_names_no_state_root_path_but_the_builders_own_checkout(self):
        # The checkout is where the builder already stands, so naming it
        # discloses nothing. Everything else under the state root -- the
        # ledger, the vault, another lane's tree -- must stay unnamed; the
        # probe resolves those itself from the run and lane ids.
        argv = self._extra()["sealed_probe_command"]

        under_state = [item for item in argv if item.startswith(str(STATE_ROOT))]
        self.assertEqual(under_state, [str(CHECKOUT)])

    def test_the_argv_names_no_vault_and_no_sealed_file(self):
        argv = self._extra()["sealed_probe_command"]

        joined = " ".join(argv)
        self.assertNotIn("vault", joined)
        self.assertNotIn("vaults/", joined)
        self.assertNotIn(".test.", joined)
        self.assertNotIn("private", joined)

    def test_the_builder_is_told_what_the_command_is_for(self):
        instruction = self._extra()["sealed_probe_instruction"]

        self.assertIn("sealed_probe_command", instruction)
        self.assertIn("working tree", instruction)
        self.assertIn("before you finish", instruction)

    def test_the_probe_survives_the_builder_prompt_guard(self):
        # `_prompt` strips forbidden private keys from a builder body and
        # refuses outright on a vault path. The argv has to come out the other
        # side intact or the builder never sees it.
        extra = self._extra()
        actor = maestro.HerdrStageActor.__new__(maestro.HerdrStageActor)
        actor.lane_specs = {}
        ctx = SimpleNamespace(
            lane=SimpleNamespace(lane_id="lane-a", lane_kind=st.LANE_KIND_BUILD),
            plan_revision=1,
            run_id="run1",
            stage=st.LaneStage.BUILDING,
        )

        body = maestro.HerdrStageActor._prompt(
            actor,
            ctx,
            "builder",
            Path("/tmp/envelope.json"),
            CHECKOUT,
            extra,
        )

        self.assertEqual(
            body["sealed_probe_command"], extra["sealed_probe_command"]
        )
        self.assertIn("before you finish", json.dumps(body))


CONTRACT = {
    "acceptance_criteria": ("a paid workspace is offered the entity DPA",),
    "declared_outputs": ("src/lib/paidDpa.ts",),
}
CONSTRAINTS = ("keep the handler pure",)
FINDING = {
    "implementation_area": "src/lib/paidDpa.ts",
    "observed_behavior": "the mapper drops the envelope keys",
    "required_behavior": "preserve the envelope keys",
    "violated_requirement": "a paid workspace is offered the entity DPA",
}
SECOND_FINDING = {
    "implementation_area": "src/lib/paidDpa.ts",
    "observed_behavior": "the module exports no entitlement helper",
    "required_behavior": "export the entitlement helper",
    "violated_requirement": "a paid workspace is offered the entity DPA",
}


class TheBuilderViewFramesTheReviewersProse(unittest.TestCase):
    """Ground truth is the runner. The findings are a reading of it."""

    @staticmethod
    def _prior(summary, findings=(FINDING,)):
        return st.LaneArtifact(
            kind=st.ArtifactKind.CODE_REVIEW,
            plan_revision=1,
            spec_digest=_digest("spec"),
            lane_projection_digest=_digest("projection"),
            input_digest=_digest("input"),
            output_digest=_digest("output"),
            artifact_ref="refs/maestro/private-results/r/l/" + _digest("results"),
            payload={
                "findings": [dict(item) for item in findings],
                "input_digest": _digest("input"),
                "public_result_summary": dict(summary),
                "redacted_failures": ["E  AssertionError: matrix has an extra key"],
                "verdict": st.ReviewerVerdict.REVISE.value,
            },
            verdict=st.ReviewerVerdict.REVISE,
        )

    def _view(self, summary, findings=(FINDING,), tokens=()):
        return cr.builder_view(
            public_contract=CONTRACT,
            architecture_constraints=CONSTRAINTS,
            sealed_digest="3" * 64,
            prior_code_review=self._prior(summary, findings),
            private_tokens=tokens,
        )

    def _findings(self, summary, findings=(FINDING,)):
        return self._view(summary, findings)["prior_code_review"]["findings"]

    def test_a_failed_case_puts_the_framing_above_the_findings(self):
        got = self._findings({"executed": 12, "passed": 7, "failed": 5, "errored": 0})

        self.assertEqual(got[0], cr._FINDINGS_FRAMING)
        self.assertEqual(got[1:], [FINDING])

    def test_an_errored_case_puts_the_framing_above_the_findings(self):
        # A collection error executes nothing and fails nothing, so a check
        # that read `failed` alone would leave the worst case unframed.
        got = self._findings({"executed": 0, "passed": 0, "failed": 0, "errored": 3})

        self.assertEqual(got[0], cr._FINDINGS_FRAMING)
        self.assertEqual(got[1:], [FINDING])

    def test_a_green_summary_is_not_framed(self):
        got = self._findings({"executed": 12, "passed": 12, "failed": 0, "errored": 0})

        self.assertEqual(got, [FINDING])

    def test_the_framing_deletes_no_finding(self):
        red = {"executed": 12, "passed": 7, "failed": 5, "errored": 0}
        green = {"executed": 12, "passed": 12, "failed": 0, "errored": 0}
        both = (FINDING, SECOND_FINDING)

        framed = self._findings(red, both)
        unframed = self._findings(green, both)

        # "You wrote outside your declared outputs" is true whatever the suite
        # says. Framing reorders authority; it never filters.
        self.assertEqual(framed[1:], [FINDING, SECOND_FINDING])
        self.assertEqual(unframed, [FINDING, SECOND_FINDING])

    def test_the_framing_names_which_side_is_ground_truth(self):
        got = self._findings({"executed": 12, "passed": 7, "failed": 5, "errored": 0})

        self.assertIn("ground truth", got[0])
        self.assertIn("redacted_failures", got[0])
        self.assertIn("one model's reading", got[0])

    def test_a_summary_missing_the_keys_is_not_framed(self):
        # Counts arrive off an artifact payload. A malformed one must not cost
        # the builder its review.
        self.assertEqual(self._findings({"passed": 7}), [FINDING])
        self.assertEqual(self._findings({"failed": "nope"}), [FINDING])

    def test_the_framing_does_not_trip_the_leak_guard(self):
        got = self._view(
            {"executed": 12, "passed": 7, "failed": 5, "errored": 0},
            tokens=("entitled by seat count", "enterprise"),
        )

        self.assertEqual(
            got["prior_code_review"]["findings"][0], cr._FINDINGS_FRAMING
        )

    def test_the_framing_carries_no_private_token(self):
        tokens = ("entitled by seat count", "enterprise", "prrCiLow")
        pr.refuse_private_leak(cr._FINDINGS_FRAMING, tokens)

    def test_no_prior_review_is_still_no_review(self):
        view = cr.builder_view(
            public_contract=CONTRACT,
            architecture_constraints=CONSTRAINTS,
            sealed_digest="3" * 64,
        )

        self.assertEqual(view["prior_code_review"], st.NO_CODE_REVIEW)


class TheFramingReachesTheBuilderPayload(unittest.TestCase):
    """`builder_view` is not the path the builder's prompt travels.

    `review_builder_output` builds the view and uses it only to assert that a
    REVISE did not lose its prior review; the findings the builder actually
    reads come off the CODE_REVIEW artifact through `_revise_findings`. So the
    framing has to be applied at both readers or it reaches nobody. Same
    constant, same predicate, so the two cannot drift apart into disagreeing
    about which of the failure lines and the findings is ground truth.
    """

    @staticmethod
    def _artifacts(summary, findings=(FINDING,)):
        return {
            "CODE_REVIEW": SimpleNamespace(
                payload={
                    "findings": [dict(item) for item in findings],
                    "public_result_summary": dict(summary),
                    "redacted_failures": ["E  AssertionError: matrix has an extra key"],
                    "verdict": st.ReviewerVerdict.REVISE.value,
                }
            )
        }

    def _findings(self, summary, findings=(FINDING,)):
        return _build_extra(self._artifacts(summary, findings))["revise_findings"]

    def test_a_red_summary_frames_the_findings_in_the_payload(self):
        got = self._findings({"executed": 12, "passed": 7, "failed": 5, "errored": 0})

        self.assertEqual(got[0], cr._FINDINGS_FRAMING)
        self.assertEqual(got[1:], [FINDING])

    def test_an_errored_summary_frames_the_findings_in_the_payload(self):
        got = self._findings({"executed": 0, "passed": 0, "failed": 0, "errored": 3})

        self.assertEqual(got[0], cr._FINDINGS_FRAMING)
        self.assertEqual(got[1:], [FINDING])

    def test_a_green_summary_frames_nothing(self):
        got = self._findings({"executed": 12, "passed": 12, "failed": 0, "errored": 0})

        self.assertEqual(got, [FINDING])

    def test_every_finding_survives_the_framing(self):
        both = (FINDING, SECOND_FINDING)
        red = {"executed": 12, "passed": 7, "failed": 5, "errored": 0}
        green = {"executed": 12, "passed": 12, "failed": 0, "errored": 0}

        self.assertEqual(self._findings(red, both)[1:], [FINDING, SECOND_FINDING])
        self.assertEqual(self._findings(green, both), [FINDING, SECOND_FINDING])

    def test_a_review_with_no_summary_frames_nothing(self):
        artifacts = {
            "CODE_REVIEW": SimpleNamespace(payload={"findings": [dict(FINDING)]})
        }

        got = _build_extra(artifacts)["revise_findings"]

        self.assertEqual(got, [FINDING])

    def test_no_prior_review_carries_no_findings_key_at_all(self):
        extra = _build_extra({})

        self.assertNotIn("revise_findings", extra)

    def test_the_payload_and_the_view_use_one_constant(self):
        # Two copies of this sentence is two sentences that can disagree. The
        # builder would then be told which side is ground truth twice, in two
        # different words, by the same review.
        summary = {"executed": 12, "passed": 7, "failed": 5, "errored": 0}
        payload_first = self._findings(summary)[0]
        view = cr.builder_view(
            public_contract=CONTRACT,
            architecture_constraints=CONSTRAINTS,
            sealed_digest="3" * 64,
            prior_code_review=TheBuilderViewFramesTheReviewersProse._prior(summary),
        )
        view_first = view["prior_code_review"]["findings"][0]

        self.assertEqual(payload_first, view_first)
        self.assertIs(payload_first, cr._FINDINGS_FRAMING)

    def test_the_framing_survives_the_builder_prompt_guard(self):
        summary = {"executed": 12, "passed": 7, "failed": 5, "errored": 0}
        extra = _build_extra(self._artifacts(summary))
        actor = maestro.HerdrStageActor.__new__(maestro.HerdrStageActor)
        actor.lane_specs = {}
        ctx = SimpleNamespace(
            lane=SimpleNamespace(lane_id="lane-a", lane_kind=st.LANE_KIND_BUILD),
            plan_revision=1,
            run_id="run1",
            stage=st.LaneStage.BUILDING,
        )

        body = maestro.HerdrStageActor._prompt(
            actor,
            ctx,
            "builder",
            Path("/tmp/envelope.json"),
            CHECKOUT,
            extra,
        )

        self.assertEqual(body["revise_findings"][0], cr._FINDINGS_FRAMING)


if __name__ == "__main__":
    unittest.main()
