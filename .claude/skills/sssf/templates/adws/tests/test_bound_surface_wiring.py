"""The builder is told the names the sealed acceptance tests bind to.

Sealing the acceptance tests is what stops the builder writing to the test.
It also stopped the builder learning the API surface the tests call. On FDAdb
a sealed vitest suite called `paidDpa.buildEntityDpaSurface()` and read an
`available` key off the result; neither name appeared anywhere in the plan,
the public contract, or the declared outputs. The builder guessed nineteen
times and the pass count never moved -- it was not failing at the behavior, it
was failing to name the function.

The rule this pins:

    Names are contract. Values are secrets.

The builder learns module specifiers, exported symbols, and result-object
keys. It never learns string literals, numbers, fixture data, or the values an
assertion compares against.

These cases cover the wiring only -- vault to `derive_bound_surface` to
`LaneContext` to the builder's prompt. `derive_bound_surface` itself is a pure
function tested by `tests/test_bound_surface.py`; here it is stubbed, so a
change to what it extracts cannot silently change what these cases prove.
"""

from __future__ import annotations

import hashlib
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

import maestro  # noqa: E402
from adw_modules import private_review as prv  # noqa: E402
from adw_modules import scheduler as sch  # noqa: E402
from adw_modules import scheduler_types as st  # noqa: E402


SURFACE = {
    "modules": [
        {
            "specifier": "src/lib/paidDpa",
            "symbols": ["buildEntityDpaSurface", "resolveEntitlement"],
        }
    ],
    "object_keys": ["available", "reason"],
}

# The sealed file the surface above was derived from. Note `'available'`: nine
# characters, quoted, so `collect_private_tokens` makes a private token of it.
SEALED_SOURCE = """
import { buildEntityDpaSurface } from 'src/lib/paidDpa';

it('offers the entity DPA to a paid workspace', () => {
  const result = buildEntityDpaSurface({ tier: 'enterprise' });
  expect(result['available']).toBe(true);
  expect(result.reason).toEqual('entitled by seat count');
});
"""


def _digest(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


class _RecordingActor:
    """Records the ctx the builder was asked with."""

    def __init__(self) -> None:
        self.seen: list[object] = []

    def build(self, ctx):
        self.seen.append(ctx)
        return {"candidate_sha": "2" * 40, "changed": True}


def _scheduler(actor=None):
    scheduler = sch.FactoryScheduler.__new__(sch.FactoryScheduler)
    scheduler.run_id = "run1"
    scheduler.store = object()
    scheduler.runtime = SimpleNamespace(path=Path("/state"))
    scheduler.target = SimpleNamespace(target_repository_root="/repo")
    scheduler.actor = actor or _RecordingActor()
    lane = SimpleNamespace(
        lane_id="lane-a",
        spec_digest=_digest("spec"),
        lane_projection_digest=_digest("projection"),
        public_acceptance=("a paid workspace is offered the entity DPA",),
        declared_outputs=("src/lib/paidDpa.ts",),
        lane_kind=st.LANE_KIND_BUILD,
        needs=(),
    )
    row = {"plan_revision": 1, "plan_digest": _digest("plan")}
    plan = SimpleNamespace(
        artifact_id="art-plan",
        kind=st.ArtifactKind.LANE_PLAN,
        plan_revision=1,
        input_digest=_digest("plan-in"),
        output_digest=_digest("plan-out"),
        artifact_ref="plan:ref",
        payload={},
    )
    sealed = SimpleNamespace(
        artifact_id="art-sealed",
        kind=st.ArtifactKind.SEALED_TEST_BUNDLE,
        plan_revision=1,
        input_digest=_digest("sealed-in"),
        output_digest=_digest("sealed-out"),
        artifact_ref="sealed:ref",
        payload={"sealed_digest": "3" * 64},
    )
    scheduler._common = lambda lane_id: (row, lane)
    scheduler._sealed_for = lambda lane_arg: sealed
    scheduler._plan_artifact_ref = lambda row_arg: "plan:ref"
    scheduler._integration_head = lambda: "1" * 40
    scheduler._dep_receipts = lambda needs: ()
    return scheduler, plan, sealed


def _drive_building(scheduler, plan, *, surface=SURFACE, files=None):
    """Run `_building` with the vault and the extractor stubbed out."""
    sealed_files = files if files is not None else {"tests/dpa.test.ts": SEALED_SOURCE}
    blobs = {path: "blob-{0}".format(index) for index, path in enumerate(sealed_files)}
    by_blob = {blobs[path]: body for path, body in sealed_files.items()}

    def _latest(_store, _run, _lane, kind, **_kwargs):
        return plan if kind is st.ArtifactKind.LANE_PLAN else None

    with mock.patch.object(sch, "_latest", side_effect=_latest), mock.patch.object(
        sch, "_record_as_lane_artifact", return_value=None
    ), mock.patch.object(
        sch.hv, "ensure_vault", return_value=Path("/state/vaults/run1")
    ), mock.patch.object(
        sch.tc, "sealed_private_files", return_value=blobs
    ), mock.patch.object(
        sch.hv, "cat_blob", side_effect=lambda _vault, blob: by_blob[blob].encode()
    ), mock.patch.object(
        sch.bsf, "derive_bound_surface", return_value=surface
    ) as derive, mock.patch.object(
        sch.gitpub,
        "admit_candidate",
        return_value={
            "builder_base_sha": "1" * 40,
            "candidate_ref": st.candidate_ref("run1", "lane-a", _digest("b")),
            "candidate_sha": "2" * 40,
            "changed": True,
            "tree_delta": [],
        },
    ), mock.patch.object(
        sch.prv, "make_lane_artifact", return_value=None
    ) as artifact, mock.patch.object(
        sch, "_complete"
    ) as complete:
        scheduler._building("lane-a")
    return derive, artifact, complete


class TheSchedulerDerivesTheSurface(unittest.TestCase):
    def test_the_builder_is_asked_with_the_bound_surface(self):
        scheduler, plan, _sealed = _scheduler()

        _drive_building(scheduler, plan)

        self.assertEqual(len(scheduler.actor.seen), 1)
        self.assertEqual(dict(scheduler.actor.seen[0].bound_surface), SURFACE)

    def test_the_extractor_reads_the_decoded_sealed_files(self):
        # The vault holds blob ids, not text. A caller that handed the blob ids
        # to the extractor would derive a surface from the string "blob-0".
        scheduler, plan, _sealed = _scheduler()

        derive, _artifact, _complete = _drive_building(scheduler, plan)

        self.assertEqual(derive.call_count, 1)
        passed = derive.call_args.args[0]
        self.assertEqual(list(passed), ["tests/dpa.test.ts"])
        self.assertIn("buildEntityDpaSurface", passed["tests/dpa.test.ts"])

    def test_every_sealed_file_is_offered_to_the_extractor(self):
        scheduler, plan, _sealed = _scheduler()
        files = {
            "tests/dpa.test.ts": SEALED_SOURCE,
            "tests/tier.test.ts": "import { resolveEntitlement } from 'src/lib/paidDpa';",
        }

        derive, _artifact, _complete = _drive_building(scheduler, plan, files=files)

        self.assertEqual(dict(derive.call_args.args[0]), files)

    def test_an_empty_surface_is_not_carried(self):
        # Nothing extracted is not an empty contract, it is no contract. Sending
        # it would tell the builder the suite binds to no names at all.
        scheduler, plan, _sealed = _scheduler()

        _drive_building(scheduler, plan, surface={"modules": [], "object_keys": []})

        self.assertIsNone(scheduler.actor.seen[0].bound_surface)

    def test_keys_alone_are_carried(self):
        scheduler, plan, _sealed = _scheduler()
        surface = {"modules": [], "object_keys": ["available"]}

        _drive_building(scheduler, plan, surface=surface)

        self.assertEqual(dict(scheduler.actor.seen[0].bound_surface), surface)

    def test_the_builder_output_artifact_carries_no_surface(self):
        # The surface is prompt input, not a durable artifact field. Putting it
        # in the payload would digest it into the lane's identity and change
        # BUILDER_OUTPUT every time the extractor changed.
        scheduler, plan, _sealed = _scheduler()

        _derive, artifact, complete = _drive_building(scheduler, plan)

        payload = artifact.call_args.kwargs["payload"]
        self.assertNotIn("bound_surface", payload)
        self.assertEqual(complete.call_count, 1)

    def test_the_lane_context_defaults_to_no_surface(self):
        field = {
            item.name: item
            for item in sch.dataclasses.fields(sch.LaneContext)
        }["bound_surface"]
        self.assertIsNone(field.default)


class TheBuilderPromptCarriesTheSurface(unittest.TestCase):
    """The names have to reach the agent's prompt, not just its ctx."""

    @staticmethod
    def _body(extra):
        actor = maestro.HerdrStageActor.__new__(maestro.HerdrStageActor)
        actor.lane_specs = {}
        ctx = SimpleNamespace(
            lane=SimpleNamespace(lane_id="lane-a", lane_kind=st.LANE_KIND_BUILD),
            plan_revision=1,
            run_id="run1",
            stage=st.LaneStage.BUILDING,
        )
        return maestro.HerdrStageActor._prompt(
            actor,
            ctx,
            "builder",
            Path("/tmp/envelope.json"),
            Path("/tmp/cwd"),
            extra,
        )

    def test_the_module_specifier_and_symbols_are_named(self):
        text = self._body({"bound_surface": SURFACE})["instructions"]
        self.assertIn("src/lib/paidDpa", text)
        self.assertIn("buildEntityDpaSurface", text)
        self.assertIn("resolveEntitlement", text)

    def test_the_result_object_keys_are_named(self):
        text = self._body({"bound_surface": SURFACE})["instructions"]
        self.assertIn("available", text)
        self.assertIn("reason", text)

    def test_the_names_are_stated_as_contract_not_suggestion(self):
        text = self._body({"bound_surface": SURFACE})["instructions"]
        self.assertIn("contract, not a suggestion", text)
        self.assertIn("exactly these names", text)

    def test_the_prompt_says_the_values_are_withheld(self):
        text = self._body({"bound_surface": SURFACE})["instructions"]
        self.assertIn("deliberately withheld", text)
        self.assertIn("Do not guess", text)
        self.assertIn("do not hardcode", text)

    def test_a_builder_without_a_surface_gets_no_instruction(self):
        text = self._body({})["instructions"]
        self.assertNotIn("bound_surface", text)
        self.assertIn("Edit only declared_outputs", text)

    def test_an_empty_surface_renders_nothing(self):
        text = self._body({"bound_surface": {"modules": [], "object_keys": []}})[
            "instructions"
        ]
        self.assertNotIn("bound_surface is the set of names", text)

    def test_a_module_with_no_symbols_is_still_named(self):
        text = self._body(
            {"bound_surface": {"modules": [{"specifier": "src/lib/paidDpa"}], "object_keys": []}}
        )["instructions"]
        self.assertIn("src/lib/paidDpa", text)
        self.assertNotIn("exports .", text)

    def test_the_surface_survives_the_private_key_strip(self):
        # `_prompt` deletes forbidden private keys from the builder's body and
        # raises PRIVATE_TEST_LEAK on a vault path. A name is neither.
        body = self._body({"bound_surface": SURFACE})
        self.assertEqual(body["bound_surface"], SURFACE)

    def test_the_code_reviewer_prompt_gets_no_builder_surface_text(self):
        actor = maestro.HerdrStageActor.__new__(maestro.HerdrStageActor)
        actor.lane_specs = {}
        ctx = SimpleNamespace(
            lane=SimpleNamespace(lane_id="lane-a", lane_kind=st.LANE_KIND_BUILD),
            plan_revision=1,
            run_id="run1",
            stage=st.LaneStage.REVIEWING_CODE,
        )
        text = maestro.HerdrStageActor._prompt(
            actor,
            ctx,
            "code-reviewer",
            Path("/tmp/envelope.json"),
            Path("/tmp/cwd"),
            {},
        )["instructions"]
        self.assertNotIn("bound_surface", text)


class TheActorPutsTheSurfaceInExtra(unittest.TestCase):
    """`build()` is the only place the surface is allowed to enter a prompt."""

    @staticmethod
    def _actor():
        actor = maestro.HerdrStageActor.__new__(maestro.HerdrStageActor)
        actor.lane_specs = {}
        actor._roles = {}
        actor._base_sha = lambda ctx, role, *rest: "1" * 40
        actor._prepare = lambda ctx, role, *, sha, private_tree: (
            Path("/tmp/attempt"),
            Path("/tmp/attempt/checkout"),
        )
        actor._bind_checkout = lambda key, attempt, checkout, used: used
        actor._commit_declared = lambda cwd, outputs, sha, *, strip=(): (
            "2" * 40,
            True,
        )
        actor._refresh_builder_checkout = lambda *a, **k: None
        return actor

    @staticmethod
    def _ctx(surface):
        return SimpleNamespace(
            lane=SimpleNamespace(
                lane_id="lane-a",
                lane_kind=st.LANE_KIND_BUILD,
                declared_outputs=("src/lib/paidDpa.ts",),
                # A real LaneProjection always carries one, and the role key is
                # keyed on it so a revised spec starts a fresh agent session.
                spec_digest="ab" * 32,
            ),
            plan_revision=1,
            run_id="run1",
            stage=st.LaneStage.BUILDING,
            public_contract={"acceptance_criteria": []},
            sealed_digest="3" * 64,
            artifacts={},
            bound_surface=surface,
            # These cases are about the prompt, not the checkout. The strip
            # guard is stubbed out above; a real `LaneContext` always carries
            # this field, so a fake that omits it fails loudly rather than
            # silently launching a builder over its own sealed suite.
            sealed_private_paths=(),
        )

    def _extra(self, surface):
        actor = self._actor()
        seen = {}

        def launch(ctx, role, cwd, extra, *, prepare_cwd):
            seen["extra"] = dict(extra)
            return {}, None, Path("/tmp/attempt/checkout")

        actor._launch = launch
        actor.build(self._ctx(surface))
        return seen["extra"]

    def test_a_surface_reaches_the_builder_extra(self):
        self.assertEqual(self._extra(SURFACE)["bound_surface"], SURFACE)

    def test_no_surface_adds_no_key(self):
        self.assertNotIn("bound_surface", self._extra(None))


class TheRolesHaveSeparateCheckouts(unittest.TestCase):
    """Nothing added to one role's prompt can land in another role's tree.

    `_prompt` writes its rendered body into the role's own checkout under
    `.maestro-agent/`. If the builder and the code reviewer shared a checkout,
    the builder's bound-surface instruction would sit on disk inside the tree
    the reviewer reads -- and the reviewer would be judging a candidate while
    holding a description of the sealed suite's surface.
    """

    @staticmethod
    def _actor():
        actor = maestro.HerdrStageActor.__new__(maestro.HerdrStageActor)
        actor.worktrees = Path("/state/worktrees")
        return actor

    def test_the_builder_and_code_reviewer_do_not_share_a_directory(self):
        actor = self._actor()
        ctx = SimpleNamespace(
            run_id="run1", lane=SimpleNamespace(lane_id="lane-a")
        )
        builder = actor._role_dir(ctx, "builder", create=False)
        reviewer = actor._role_dir(ctx, "code-reviewer", create=False)

        self.assertNotEqual(builder, reviewer)
        self.assertNotIn(builder, reviewer.parents)
        self.assertNotIn(reviewer, builder.parents)
        self.assertEqual(builder.name, "builder")
        self.assertEqual(reviewer.name, "code-reviewer")

    def test_every_lane_role_gets_its_own_checkout(self):
        actor = self._actor()
        ctx = SimpleNamespace(
            run_id="run1", lane=SimpleNamespace(lane_id="lane-a")
        )
        cwds = actor._role_cwds(ctx)

        self.assertEqual(len(set(cwds.values())), len(cwds))
        self.assertIn("builder", cwds)
        self.assertIn("code-reviewer", cwds)
        self.assertNotEqual(cwds["builder"], cwds["code-reviewer"])


class TheLeakGuardAllowsNamesAndStillHidesValues(unittest.TestCase):
    """The trap: a bound name that is also a quoted literal in the test.

    `collect_private_tokens` makes a token of every quoted literal of eight
    characters or more in a sealed file. `result['available']` puts the
    nine-character literal `available` in that set -- so the key the builder
    must implement is, by that measure, a private token.

    It reaches the builder anyway, and these cases pin exactly why, so that a
    later change to either guard shows up here rather than in a lane that
    starts guessing again:

      * `_prompt`'s builder guard is key-based, not token-based. It deletes
        forbidden private keys and refuses a vault path; it never compares the
        body against the sealed file's tokens.
      * `_building`'s `refuse_private_leak` checks the BUILDER_OUTPUT payload,
        not the prompt, and against only the tokens the builder itself
        returned -- so a name in the prompt is out of its scope by
        construction.

    Neither guard is weakened here. The values stay hidden: the same sealed
    file yields tokens for `'entitled by seat count'` and the whole assertion
    line, and none of those appear in the surface or the prompt.
    """

    def _tokens(self):
        return prv.collect_private_tokens(files={"tests/dpa.test.ts": SEALED_SOURCE})

    def test_the_bound_key_really_is_a_private_token(self):
        # If this stops being true the trap is gone and so is the reason for
        # the cases below.
        self.assertIn("available", self._tokens())

    def test_the_bound_key_still_reaches_the_builder_prompt(self):
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
            Path("/tmp/cwd"),
            {"bound_surface": SURFACE},
        )
        self.assertIn("available", body["instructions"])
        self.assertEqual(body["bound_surface"], SURFACE)

    def test_building_does_not_refuse_a_candidate_over_a_bound_name(self):
        scheduler, plan, _sealed = _scheduler()

        _derive, _artifact, complete = _drive_building(scheduler, plan)

        self.assertEqual(complete.call_count, 1)

    def test_the_expected_values_are_not_in_the_surface(self):
        rendered = maestro.HerdrStageActor._bound_surface_instruction(SURFACE)
        self.assertNotIn("entitled by seat count", rendered)
        self.assertNotIn("enterprise", rendered)
        self.assertNotIn("toBe(true)", rendered)

    def test_those_values_are_private_tokens_and_stay_that_way(self):
        # The names are tokens too -- a module specifier is quoted in an import
        # and a key is quoted in a subscript -- so the claim worth pinning is
        # the exact one: the ONLY private tokens in the builder's instruction
        # are the names the surface declares. Anything else appearing there is
        # a value that escaped.
        tokens = self._tokens()
        self.assertIn("entitled by seat count", tokens)
        rendered = maestro.HerdrStageActor._bound_surface_instruction(SURFACE)
        names = set(SURFACE["object_keys"])
        for entry in SURFACE["modules"]:
            names.add(entry["specifier"])
            names.update(entry["symbols"])
        leaked = [
            token for token in tokens if token not in names and token in rendered
        ]
        self.assertEqual(leaked, [])


if __name__ == "__main__":
    unittest.main()
