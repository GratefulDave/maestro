"""Pin this runtime's closed vocabularies to the published Plan IR schema.

Plan Contract's `planctl validate` and this runtime's admission predicates
deliberately check overlapping things. The review receipt is bound to the IR
bytes, so a field cannot be added to a plan after approval, and every refusal
this runtime can raise at ingress therefore has to be reachable while the plan
is still authorable. That is why `planctl.py` re-implements the well-formedness
half of `validate_contract_surface` and `validate_effect_authorization`.

Mirrored checks are only safe while both sides mean the same thing by each enum
member. A sixth effect added to `EFFECTS` and not to `$defs.effectName.enum` --
or the reverse -- reproduces exactly the deadlock those fields were added to
close: a plan that declares the new member passes one gate and fails the other,
with no route that passes both. Nothing else in either repository notices,
because each suite is green against its own copy of the vocabulary. This module
is the only thing standing between that edit and a production plan that cannot
be authored.

The schema is published from `the-library`. When this runtime is the-library's
own template checkout the schema is in the same repository; when it is
maestro's, the schema is in the peer checkout beside it. Both are resolved
here, so the two template copies hold identical bytes and `test_template_parity`
compares them without a special case.

Skips only when no checkout carrying the schema is present at all. A checkout
that is present but missing the schema fails: that is the silent-deletion mode
the parity module exists to catch, one artifact over.

The peer checkout is resolved through `checkout_layout`, from the repository
rather than from this file's path, because every lane authors inside a linked
worktree and a peer has never existed beside one. Resolving it from the
filesystem meant this module skipped through exactly the edits it guards.
"""

from __future__ import annotations

import ast
import json
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import checkout_layout  # noqa: E402  (needs the path above)

# Where the published Plan IR schema lives inside a checkout that carries it,
# keyed by repository directory name. Only the-library publishes it; maestro
# reaches across to that checkout.
SCHEMA_LOCATIONS = (
    ("the-library", pathlib.PurePosixPath("skills/plan-contract/schemas/plan-ir-v1.schema.json")),
)

PLAN_VALIDATE = pathlib.Path(__file__).resolve().parent.parent / "adw_modules" / "plan_validate.py"

# The pinned pairs: the schema pointer, a reader for it, and the runtime
# constant it must equal. A vocabulary added to either side without a row here
# is unpinned, which is the state this module exists to end.
PINNED = (
    (
        "$defs.effectName.enum",
        lambda defs: defs["effectName"]["enum"],
        "EFFECTS",
    ),
    (
        "$defs.effectDisposition.enum",
        lambda defs: defs["effectDisposition"]["enum"],
        "DISPOSITIONS",
    ),
    (
        "$defs.requirementSurfaceEntry.properties.mutation.enum",
        lambda defs: defs["requirementSurfaceEntry"]["properties"]["mutation"]["enum"],
        "MUTATIONS",
    ),
)


def _resolve_schema():
    """Return the published schema path, or raise SkipTest / AssertionError."""
    checkout = checkout_layout.identify_template_checkout(
        pathlib.Path(__file__).resolve().parent.parent
    )
    if checkout is None:
        checkout_layout.skip_visibly(
            "this ADW runtime is a deployed instance, not a template checkout, "
            "so it has no published schema to be pinned to; the check runs in "
            "the maestro and the-library repositories"
        )

    reasons = []
    for repo_name, relative in SCHEMA_LOCATIONS:
        repo_root = checkout_layout.checkout_root(checkout, repo_name)
        schema = repo_root / relative
        if schema.is_file():
            return schema
        if repo_root.is_dir():
            # The repository publishing the schema is checked out but the schema
            # is gone. Skipping here would leave the vocabularies unpinned and
            # silent, which is the failure this module exists to prevent.
            raise AssertionError(
                "{repo} is checked out at {root} but the published Plan IR "
                "schema {schema} is missing, so this runtime's vocabularies are "
                "pinned to nothing. Restore the schema before landing anything "
                "that touches EFFECTS, DISPOSITIONS, or MUTATIONS. "
                "({provenance})".format(
                    repo=repo_name,
                    root=repo_root,
                    schema=schema,
                    provenance=checkout.provenance,
                )
            )
        reasons.append(
            "{name} is not checked out at {path}".format(name=repo_name, path=repo_root)
        )

    checkout_layout.skip_visibly(
        "no checkout publishing the Plan IR schema is present on this machine "
        "({reasons}); vocabulary parity cannot be checked from this checkout "
        "alone. {provenance}".format(
            reasons="; ".join(reasons), provenance=checkout.provenance
        )
    )


def module_string_tuple(source, name):
    """Read a module-level tuple-of-strings constant without importing it.

    `plan_validate` reaches its siblings through package-relative imports, so
    importing it would pull the whole runtime into this test to read three
    literals. Parsing is also what makes the failure honest: a constant that
    stops being a literal tuple raises here rather than quietly resolving to
    something this test cannot compare against a JSON enum.
    """
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    for node in tree.body:
        if isinstance(node, ast.AnnAssign):
            targets = [node.target]
        elif isinstance(node, ast.Assign):
            targets = node.targets
        else:
            continue
        if not any(isinstance(target, ast.Name) and target.id == name for target in targets):
            continue
        if node.value is None:
            break
        try:
            value = ast.literal_eval(node.value)
        except ValueError as exc:
            raise AssertionError(
                "{name} in {source} is no longer a literal tuple, so the schema "
                "can no longer be pinned to it: {exc}".format(
                    name=name, source=source, exc=exc
                )
            )
        if not isinstance(value, tuple) or not all(isinstance(item, str) for item in value):
            raise AssertionError(
                "{name} in {source} is not a tuple of strings: {value!r}".format(
                    name=name, source=source, value=value
                )
            )
        return value
    raise AssertionError(
        "{name} is not defined at module level in {source}; the schema is "
        "pinned to a constant that no longer exists".format(name=name, source=source)
    )


class SchemaVocabularyParityTests(unittest.TestCase):
    def setUp(self):
        self.schema_path = _resolve_schema()
        self.defs = json.loads(self.schema_path.read_text(encoding="utf-8"))["$defs"]

    def assert_vocabulary_matches(self, pointer, schema_members, constant):
        runtime = module_string_tuple(PLAN_VALIDATE, constant)
        schema = tuple(schema_members)
        schema_only = sorted(set(schema) - set(runtime))
        runtime_only = sorted(set(runtime) - set(schema))
        if schema_only or runtime_only:
            lines = [
                "{pointer} and {constant} declare different vocabularies.".format(
                    pointer=pointer, constant=constant
                )
            ]
            if schema_only:
                lines.append(
                    "  in the schema but not the runtime: " + ", ".join(schema_only)
                )
            if runtime_only:
                lines.append(
                    "  in the runtime but not the schema: " + ", ".join(runtime_only)
                )
            lines.append("  schema  ({0}): {1}".format(self.schema_path, list(schema)))
            lines.append("  runtime ({0}): {1}".format(PLAN_VALIDATE, list(runtime)))
            lines.append(
                "  A member on one side only is the deadlock this pin exists to "
                "prevent: a plan declaring it passes one gate and fails the "
                "other. Add it to both, or to neither."
            )
            self.fail("\n".join(lines))
        self.assertEqual(
            len(schema),
            len(set(schema)),
            "{0} repeats a member: {1}".format(pointer, list(schema)),
        )
        self.assertEqual(
            len(runtime),
            len(set(runtime)),
            "{0} repeats a member: {1}".format(constant, list(runtime)),
        )
        self.assertEqual(
            len(schema),
            len(runtime),
            "{0} declares {1} members and {2} declares {3}".format(
                pointer, len(schema), constant, len(runtime)
            ),
        )
        self.assertEqual(
            schema,
            runtime,
            "{0} and {1} agree on membership but not on order: {2} vs {3}. The "
            "order is pinned so a reader of either artifact sees the same "
            "vocabulary.".format(pointer, constant, list(schema), list(runtime)),
        )

    def test_the_runtime_is_present_to_be_compared_against(self):
        """An absent runtime fails rather than vacuously passing every check."""
        self.assertTrue(
            PLAN_VALIDATE.is_file(),
            "{0} is missing, so the schema's vocabularies are pinned to "
            "nothing".format(PLAN_VALIDATE),
        )

    def test_the_effect_vocabulary_matches_the_published_schema(self):
        pointer, reader, constant = PINNED[0]
        self.assert_vocabulary_matches(pointer, reader(self.defs), constant)

    def test_the_disposition_vocabulary_matches_the_published_schema(self):
        pointer, reader, constant = PINNED[1]
        self.assert_vocabulary_matches(pointer, reader(self.defs), constant)

    def test_the_surface_mutation_vocabulary_matches_the_published_schema(self):
        pointer, reader, constant = PINNED[2]
        self.assert_vocabulary_matches(pointer, reader(self.defs), constant)

    def test_every_pinned_row_names_a_constant_this_runtime_defines(self):
        """A row whose constant vanished must fail rather than silently drop."""
        for _pointer, _reader, constant in PINNED:
            with self.subTest(constant=constant):
                self.assertTrue(module_string_tuple(PLAN_VALIDATE, constant))


if __name__ == "__main__":
    unittest.main()
