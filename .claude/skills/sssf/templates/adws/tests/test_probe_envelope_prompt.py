"""The code reviewer's probe envelope and prompt, and the stash behind `take_probes`.

Real `HerdrStageActor`, real git worktrees, real `probes.parse_envelope` and
`probes.read_submissions`; only the launcher is scripted.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import maestro
from adw_modules import probes
from adw_modules.scheduler import FactoryRefused

from tests import probe_actor_bench as pb

PROBE = "probes/test_negative.py"
PROBE_SOURCE = "def test_negative():\n    assert False\n"


def _reviewer(envelope_extra: Mapping[str, Any], *, write: bool = True):
    def respond(spec: Any, prompt: Mapping[str, Any]) -> Mapping[str, Any]:
        if write:
            target = Path(spec.worktree) / PROBE
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(PROBE_SOURCE, encoding="utf-8")
        return {"verdict": "PASS", "findings": [], **envelope_extra}

    return respond


_ENTRY = {
    "path": PROBE,
    "requirement": pb.CONTRACT_QUOTE,
    "intent": "negative quantity is refused",
}


class SchemaTest(unittest.TestCase):
    def test_probes_and_challenges_are_one_object_each_inside_lists(self) -> None:
        schema = maestro.HerdrStageActor._schema(None, "code-reviewer", with_probes=True)  # type: ignore[arg-type]
        self.assertEqual(len(schema["probes"]), 1)
        self.assertEqual(set(schema["probes"][0]), {"path", "requirement", "intent"})
        self.assertEqual(len(schema["challenges"]), 1)
        self.assertEqual(set(schema["challenges"][0]), {"probe_id", "reason"})
        self.assertEqual(len(schema["findings"]), 1)

    def test_default_schema_is_unchanged(self) -> None:
        schema = maestro.HerdrStageActor._schema(None, "code-reviewer")  # type: ignore[arg-type]
        self.assertEqual(set(schema), {"verdict", "findings"})

    def test_acceptance_schema_lists_one_probe_verdict_object(self) -> None:
        schema = maestro.HerdrStageActor._schema(  # type: ignore[arg-type]
            None, "test-reviewer", mode=probes.MODE_ACCEPT
        )
        self.assertEqual(set(schema), {"verdict", "probe_verdicts"})
        self.assertEqual(
            set(schema["probe_verdicts"][0]), {"probe_id", "verdict", "reason"}
        )


class PromptTest(unittest.TestCase):
    def _prompt(self, *, vendors: Any, budget: int, **fields: Any) -> Mapping[str, Any]:
        with tempfile.TemporaryDirectory() as tmp:
            bench = pb.Bench(tmp, _reviewer({}, write=False), role_vendors=vendors)
            bench.actor.review_code(
                pb.context(bench.head, probe_budget_left=budget, **fields)
            )
            return bench.launcher.prompts[0]

    def test_mandate_carries_the_authoring_rules(self) -> None:
        prompt = self._prompt(vendors=pb.DIFFERENT_VENDORS, budget=3)
        text = prompt["instructions"]
        self.assertIn("Probes.", text)
        self.assertIn("verbatim quote of public_contract", text)
        self.assertIn("import only the modules and names the public contract's interface declares", text)
        self.assertIn("written as files in THIS checkout", text)
        self.assertIn("probes", prompt["envelope_schema"])
        self.assertIn("challenges", prompt["envelope_schema"])

    def test_mandate_is_omitted_when_the_budget_is_spent_or_off(self) -> None:
        prompt = self._prompt(vendors=pb.DIFFERENT_VENDORS, budget=0)
        self.assertNotIn("Probes.", prompt["instructions"])
        self.assertNotIn("probes", prompt["envelope_schema"])

    def test_mandate_is_omitted_without_an_independent_acceptor(self) -> None:
        for vendors in (
            None,
            {"code-reviewer": "openai", "test-reviewer": "openai", "tester": "openai"},
            {"test-reviewer": "anthropic"},
        ):
            prompt = self._prompt(vendors=vendors, budget=3)
            self.assertNotIn("Probes.", prompt["instructions"], vendors)

    def test_probe_less_dispatch_carries_no_probe_keys(self) -> None:
        prompt = self._prompt(vendors=None, budget=0)
        for key in ("existing_probes", "rejected_probes", "mode", "probes"):
            self.assertNotIn(key, prompt)

    def test_existing_and_rejected_probes_reach_the_reviewer(self) -> None:
        prompt = self._prompt(
            vendors=pb.DIFFERENT_VENDORS,
            budget=3,
            existing_probes=(
                {"probe_id": "p-1", "path": "probes/old.py", "requirement": "r",
                 "intent": "i", "source": "SECRET-SOURCE"},
            ),
            rejected_probes=(probes.Rejection("probes/bad.py", probes.PROBE_FILE_MISSING, "gone"),),
        )
        self.assertEqual(
            prompt["existing_probes"],
            [{"probe_id": "p-1", "path": "probes/old.py", "requirement": "r", "intent": "i"}],
        )
        self.assertEqual(prompt["rejected_probes"][0]["code"], probes.PROBE_FILE_MISSING)
        self.assertIn("do not resubmit", prompt["instructions"])
        self.assertIn("refused", prompt["instructions"])


class StashTest(unittest.TestCase):
    def _run(self, envelope: Mapping[str, Any], *, write: bool = True, **fields: Any):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        bench = pb.Bench(tmp.name, _reviewer(envelope, write=write), role_vendors=pb.DIFFERENT_VENDORS)
        ctx = pb.context(bench.head, probe_budget_left=3, **fields)
        result = bench.actor.review_code(ctx)
        return bench, ctx, result

    def test_review_code_return_shape_is_unchanged(self) -> None:
        _bench, _ctx, result = self._run({"probes": [_ENTRY]})
        verdict, findings = result
        self.assertEqual(verdict.value, "PASS")
        self.assertEqual(list(findings), [])

    def test_take_probes_returns_the_authored_probe_once(self) -> None:
        bench, ctx, _ = self._run({"probes": [_ENTRY], "challenges": [{"probe_id": "p-1", "reason": "wrong"}]})
        submissions, rejections, challenges = bench.actor.take_probes(ctx)
        self.assertEqual(rejections, ())
        self.assertEqual(
            submissions,
            (probes.ProbeSubmission(PROBE, PROBE_SOURCE, pb.CONTRACT_QUOTE, "negative quantity is refused"),),
        )
        self.assertEqual(challenges, (probes.Challenge("p-1", "wrong"),))
        self.assertEqual(bench.actor.take_probes(ctx), ((), (), ()))

    def test_stash_is_keyed_by_lane_and_input_digest(self) -> None:
        bench, ctx, _ = self._run({"probes": [_ENTRY]})
        other_input = pb.context(bench.head, input_digest="66" * 32)
        other_lane = pb.context(bench.head, lane_id="lane-b")
        self.assertEqual(bench.actor.take_probes(other_input), ((), (), ()))
        self.assertEqual(bench.actor.take_probes(other_lane), ((), (), ()))
        self.assertEqual(len(bench.actor.take_probes(ctx)[0]), 1)

    def test_unread_and_refused_entries_are_typed_rejections(self) -> None:
        bench, ctx, _ = self._run(
            {
                "probes": [
                    {**_ENTRY, "path": "probes/missing.py"},
                    {**_ENTRY, "path": pb.OUTPUT},
                    {"path": "x"},
                ]
            },
            write=False,
        )
        submissions, rejections, _ = bench.actor.take_probes(ctx)
        self.assertEqual(submissions, ())
        self.assertEqual(
            sorted(item.code for item in rejections),
            sorted([probes.PROBE_FILE_MISSING, probes.PROBE_PATH_REFUSED, probes.PROBE_ENVELOPE_MALFORMED]),
        )

    def test_an_existing_probe_path_is_reserved(self) -> None:
        bench, ctx, _ = self._run(
            {"probes": [_ENTRY]},
            existing_probes=({"probe_id": "p-1", "path": PROBE},),
        )
        submissions, rejections, _ = bench.actor.take_probes(ctx)
        self.assertEqual(submissions, ())
        self.assertEqual([item.code for item in rejections], [probes.PROBE_PATH_REFUSED])

    def test_a_symlinked_probe_is_unsafe_and_never_followed(self) -> None:
        def respond(spec: Any, prompt: Mapping[str, Any]) -> Mapping[str, Any]:
            target = Path(spec.worktree) / PROBE
            target.parent.mkdir(parents=True, exist_ok=True)
            outside = Path(spec.worktree).parent / "outside.py"
            outside.write_text("secret\n", encoding="utf-8")
            os.symlink(outside, target)
            return {"verdict": "PASS", "findings": [], "probes": [_ENTRY]}

        with tempfile.TemporaryDirectory() as tmp:
            bench = pb.Bench(tmp, respond, role_vendors=pb.DIFFERENT_VENDORS)
            ctx = pb.context(bench.head, probe_budget_left=3)
            bench.actor.review_code(ctx)
            submissions, rejections, _ = bench.actor.take_probes(ctx)
        self.assertEqual(submissions, ())
        self.assertEqual([item.code for item in rejections], [probes.PROBE_FILE_UNSAFE])

    def test_no_probes_key_stashes_nothing_to_take(self) -> None:
        bench, ctx, _ = self._run({})
        self.assertEqual(bench.actor.take_probes(ctx), ((), (), ()))


class ReviewPayloadTest(unittest.TestCase):
    _FINDING = {key: "x" for key in maestro.st.REVISE_FINDING_KEYS}

    def _actor(self) -> maestro.HerdrStageActor:
        return maestro.HerdrStageActor.__new__(maestro.HerdrStageActor)

    def test_reviewer_submitted_code_is_refused(self) -> None:
        payload = {"verdict": "REVISE", "findings": [{**self._FINDING, "code": probes.PROBE_FAILED}]}
        with self.assertRaises(FactoryRefused) as caught:
            self._actor()._review_payload(payload)
        self.assertIn("REVIEW_FINDINGS_MALFORMED", str(caught.exception))

    def test_an_ordinary_finding_still_passes(self) -> None:
        verdict, findings = self._actor()._review_payload(
            {"verdict": "REVISE", "findings": [dict(self._FINDING)]}
        )
        self.assertEqual(verdict.value, "REVISE")
        self.assertEqual(len(findings), 1)


if __name__ == "__main__":
    unittest.main()
