"""§6.5's unrepresentability claim, over the whole report schema.

§6.5: the reviewer emits only per-cell `clear|finding` plus a message; it
**cannot** emit a verdict and it **cannot** emit a severity. `finalization.py`
calls "cannot" load-bearing and says how it is achieved — the report models
carry no such field and set `extra="forbid"`, "so a report containing either
is rejected at parse rather than parsed-and-ignored".

`find_forbidden_report_fields` is the detector for that claim, and
`tests/test_finalization.py` hands it two classes by name. Two gaps, both the
same family as the git-absence rule:

1. **It is handed its subjects.** A third report-shaped model added later is
   simply not passed to it, and the guard reports clean. This file discovers
   the subjects instead: it finds the report roots by looking for what the
   finalization surface actually parses, and walks each root's whole schema.
2. **It checks one half of a two-half property.** It proves no model
   *declares* `verdict` or `severity`. Nothing in the detector proved that no
   model would *accept* one. Drop `extra="forbid"` and the name check still
   returns `[]` while a reviewer's `verdict: "PASS"` is accepted and silently
   discarded — "cannot emit a verdict" becomes "may emit one, and nobody
   reads it yet", which is a model-authored field one refactor away from
   being read.

   Stated precisely, because the first draft of this docstring overstated it:
   the accepting half *is* covered today, by two behavioural tests in
   `tests/test_finalization.py` that hand-build a payload for `ReportCell`
   and one for `ReviewerReport`. Those convict a dropped `extra="forbid"` on
   exactly the two models they name — which is the same shape as gap 1. A
   third model added to the schema is covered by neither them nor the
   detector. What follows turns two spot-checks into one schema-wide
   property.

**Narrowing was not fiddly here, unlike the git rule.** The report schema has
an exact structural definition — the transitive closure of what
`ReviewerReport.model_validate` is called on — so subject discovery needs no
heuristic and no allowlist. Every model in `finalization.py` that parses
reviewer bytes is in scope and every model that does not is out of it, by
construction rather than by judgement. A `DerivedVerdict` legitimately
carrying a severity is out of scope because code derives it and no reviewer
payload parses into it, which is the same distinction, decided by the same
rule.
"""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path
from typing import List, Optional

import pydantic

ADWS = Path(__file__).resolve().parents[1]
if str(ADWS) not in sys.path:
    sys.path.insert(0, str(ADWS))

from adw_modules import code_review as cr
from adw_modules import finalization as fin

#: The modules that make up the review surface. `code_review.py` is in the
#: list because it is where a reviewer's payload is parsed: `plan finalize`
#: stopped dispatching a reviewer and `finalization.finalize` was deleted with
#: it, taking the only `ReviewerReport.model_validate` call in the runtime.
#: Discovery kept working and found nothing, which is the failure mode this
#: file's own docstring warns about — a guard that runs over an empty set is
#: green for the wrong reason. So the surface is where the parse is.
FINALIZATION_MODULES = ("finalization.py", "finalization_window.py",
                        "code_review.py")

#: Where a discovered root name is resolved. `CodeReviewerReport` lives in
#: `code_review`; its cells and the shared primitives live in `finalization`.
ROOT_MODULES = (fin, cr)


def _parse_roots() -> List[str]:
    """Model names the finalization surface parses external payloads into.

    Discovery, not a list: an `X.model_validate(...)` call in these modules
    means `X` receives bytes somebody else authored, which is exactly what
    makes it a report root and exactly what §6.5 constrains.
    """
    roots = []
    for name in FINALIZATION_MODULES:
        path = ADWS / "adw_modules" / name
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=name)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute):
                continue
            if func.attr not in ("model_validate", "model_validate_json"):
                continue
            owner = func.value
            owner_name = (owner.attr if isinstance(owner, ast.Attribute)
                          else getattr(owner, "id", None))
            if owner_name and owner_name[:1].isupper():
                roots.append(owner_name)
    return sorted(set(roots))


def find_permissive_report_models(model) -> List[str]:
    """Every model in `model`'s schema that would accept an unknown key.

    The other half of §6.5's unrepresentability claim.
    `finalization.find_forbidden_report_fields` proves that no model in the
    schema *declares* a `verdict` or a `severity`; this proves that none of
    them would *accept* one.

    Deliberately here rather than beside its sibling in `finalization.py`.
    The four detectors that do live in production are shared primitives and
    each carries an `ALLOWED` entry in `tests/test_no_dead_seams.py` saying
    why it has no production caller. This one has exactly one consumer — the
    assertion below — so putting it in production would buy a fifth
    suppression line for nothing. The discovery primitive it is built on,
    `finalization.report_schema_closure`, does live in production, because
    `find_forbidden_report_fields` genuinely uses it.
    """
    return [current.__name__
            for current in fin.report_schema_closure(model)
            if current.model_config.get("extra") != "forbid"]


def _resolve_root(name):
    for module in ROOT_MODULES:
        candidate = getattr(module, name, None)
        if (isinstance(candidate, type)
                and issubclass(candidate, pydantic.BaseModel)):
            return candidate
    return None


def _root_models():
    found = [_resolve_root(name) for name in _parse_roots()]
    return [model for model in found if model is not None]


class ReportSchemaIsDiscoveredNotListedTest(unittest.TestCase):

    def test_the_report_root_is_found_rather_than_named(self):
        self.assertIn(
            "CodeReviewerReport", _parse_roots(),
            "the review surface's parse root was not discovered — the "
            "checks below would run over nothing")

    def test_discovery_finds_a_model_rather_than_only_a_name(self):
        """The control for the check above, and for the way this test broke.

        A name that resolves to nothing leaves `_root_models()` empty, and
        every assertion below an empty loop passes. So the discovery has to
        produce a model, not just a string.
        """
        self.assertTrue(_root_models())
        self.assertIn(cr.CodeReviewerReport, _root_models())

    def test_the_closure_reaches_past_the_root(self):
        """A cell is reached through the report's `cells`, so a guard pointed
        at the root alone still covers it. That is the property that makes
        discovery sufficient."""
        closure = fin.report_schema_closure(cr.CodeReviewerReport)
        self.assertIn(cr.CodeReportCell, closure)

    def test_a_model_added_to_the_schema_is_covered_without_being_listed(self):
        """The gap this file closes. A third report-shaped model reached from
        the root must be checked because it is reachable, not because someone
        remembered to add it to a test."""

        class Attachment(pydantic.BaseModel):
            model_config = pydantic.ConfigDict(extra="forbid")
            severity: str

        class GrownCell(pydantic.BaseModel):
            model_config = pydantic.ConfigDict(extra="forbid")
            check_id: str
            attachment: Optional[Attachment] = None

        class GrownReport(pydantic.BaseModel):
            model_config = pydantic.ConfigDict(extra="forbid")
            cells: List[GrownCell]

        self.assertIn(Attachment, fin.report_schema_closure(GrownReport))
        self.assertEqual(
            ["severity"], fin.find_forbidden_report_fields(GrownReport),
            "a forbidden field two levels below the root went unseen")


class NoReportModelDeclaresAVerdictTest(unittest.TestCase):

    def test_every_discovered_root_is_clean(self):
        offenders = []
        for model in _root_models():
            for field in fin.find_forbidden_report_fields(model):
                offenders.append("{}: {}".format(model.__name__, field))
        self.assertEqual(
            [], offenders,
            "§6.5: the reviewer cannot emit a verdict and cannot emit a "
            "severity. A report model declaring one makes a confident PASS "
            "something a reviewer can say.\n" + "\n".join(offenders))


class NoReportModelAcceptsAVerdictTest(unittest.TestCase):
    """The accepting half, as a property of the schema rather than as two
    hand-built payloads for two hand-named models."""

    def test_every_model_in_the_schema_forbids_unknown_keys(self):
        offenders = []
        for model in _root_models():
            offenders.extend(find_permissive_report_models(model))
        self.assertEqual(
            [], sorted(set(offenders)),
            "a report model accepts unknown keys, so a reviewer's `verdict` "
            "is parsed-and-discarded rather than rejected. §6.5's claim is "
            "unrepresentability, not politeness: an accepted-and-ignored "
            "field is one refactor away from being read.\n"
            + "\n".join(sorted(set(offenders))))

    def test_the_check_convicts_a_permissive_model(self):
        """§13.4 — a detector returning zero proves nothing until it has been
        shown to return non-zero on a planted violation."""

        class PermissiveCell(pydantic.BaseModel):
            check_id: str

        class PermissiveReport(pydantic.BaseModel):
            model_config = pydantic.ConfigDict(extra="forbid")
            cells: List[PermissiveCell]

        self.assertEqual(["PermissiveCell"],
                         find_permissive_report_models(PermissiveReport))

    def test_a_permissive_model_really_does_swallow_a_verdict(self):
        """The behaviour behind the check, so it is not merely a config
        assertion: the field name check stays green while the reviewer's
        verdict is accepted."""

        class PermissiveCell(pydantic.BaseModel):
            check_id: str

        self.assertEqual([], fin.find_forbidden_report_fields(PermissiveCell))
        parsed = PermissiveCell.model_validate(
            {"check_id": "c", "verdict": "PASS", "severity": "ERROR"})
        self.assertEqual(parsed.check_id, "c")
        self.assertFalse(hasattr(parsed, "verdict"))

    def test_the_real_models_reject_it_instead(self):
        """The control: what the real schema does with the same payload."""
        with self.assertRaises(pydantic.ValidationError):
            fin.ReportCell.model_validate(
                {"check_id": "c", "object_id": "o", "status": "clear",
                 "message": "", "verdict": "PASS"})


if __name__ == "__main__":
    unittest.main()
