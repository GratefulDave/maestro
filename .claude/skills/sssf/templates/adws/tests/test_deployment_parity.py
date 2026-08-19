"""Make deployment drift report itself instead of waiting to be looked for.

`tests/test_template_parity.py` holds the two *template* checkouts together, and
that pair is enforced. The deployed instances are not. They are level only for as
long as somebody remembers to run `tools/runtime_sync.py`, and what that costs
has already been measured: `lexgenius/adws/adw_modules/code_review.py` sat 639
lines behind the template long enough to corrupt a real run's reviews, answering
`0` to `grep -c "def _node_goal"` while every reviewer in that deployment judged
each node against a placeholder rather than against its instruction. Nothing in
the run reported it, because nothing was looking.

This module looks, on every suite run. It does not write.

## Opt-in, and what happens on a machine with no deployments

Every path here comes from a registry a person wrote — `<repository>/.maestro/
deployments.json`, or wherever ``MAESTRO_DEPLOYMENT_REGISTRY`` points. Nothing
is hardcoded, because this suite runs in CI, in fresh clones and on machines
that have never installed the factory, and a hardcoded path would fail there for
being right about nowhere. No registry means no declared deployments, which
skips. A declared deployment that is not on this machine skips too. Both skips
name the exact path that was looked for and why the conclusion was drawn, via
`checkout_layout.skip_visibly`, because a silent skip is what hid the
wrongly-resolved peer path for the whole life of `test_template_parity`.

## The three findings, which are not interchangeable

Each declared deployment gets its own test class carrying three checks, and they
are separate because they call for three different actions:

* **A file exists in one copy and not the other.** That is a deletion, not an
  edit, and it is the shape in which 6,009 lines of runtime were once lost. It
  gets its own check and its own field in the report so it can never be read as
  "some files differ".
* **The template is ahead.** The deployment is behind and mirroring repairs it.
  The failure prints the command.
* **The deployment is ahead.** Work exists in one copy only. This is issue #71:
  twelve files where reconciling in *either* direction without reading both
  would either destroy the only copy or import one installation's local
  decisions into the runtime every installation ships. The failure says so and
  offers no command, because there is no safe one. Files whose line counts match
  but whose bytes differ are reported here too — "we cannot show the template is
  newer" is a reason for a person to look, never a licence to overwrite.

A deployment that legitimately owns a file says so in its registry entry's
`pinned` list, and the report names what it held out. That is the third option
issue #71 puts against each of the twelve, and it is the only one of the three
this mechanism can express.

## What this deliberately does not do

It never mirrors. A deployment is a live checkout with other people's in-flight
work in it: on 2026-08-19 an agent running ordinary branch hygiene in one of
them destroyed a patch with `git restore --staged --worktree`, and the bytes
survived only because an unrelated `git add` had happened to put them in the
object store minutes before. An unprompted automatic write into such a
repository is that incident with a larger blast radius. Detection is what is
automatic here. The write stays a command a person types, having read what this
told them.

Run:  uv run adw_test.py -k deployment_parity
"""

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
import warnings

ADWS = pathlib.Path(__file__).resolve().parent.parent
for _path in (str(ADWS / "tests"), str(ADWS / "tools")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import checkout_layout                                       # noqa: E402
import runtime_sync                                          # noqa: E402


def mirror_command(template_root, entry):
    """The exact command that repairs a deployment that is behind.

    Printed rather than run. It carries the entry's `pinned` paths as `--pin`
    flags so the command reproduces the comparison that failed, instead of a
    slightly different one that refuses files this check deliberately ignores.
    """
    pins = "".join(" --pin {rel}".format(rel=rel) for rel in entry.pinned)
    return (
        "python3 tools/runtime_sync.py check {src} {dst}{pins}   # then, having "
        "read it: ... mirror {src} {dst}{pins} --apply".format(
            src=template_root, dst=entry.root, pins=pins
        )
    )


def deployment_ahead_advice(entry):
    """Why a deployment that is ahead has no command attached."""
    return (
        "Work exists in {name} and not in the template. Reconciling it in "
        "either direction without reading both copies would destroy the only "
        "copy of that work or import one installation's local decisions into "
        "the runtime every installation ships, so no command is offered here. "
        "For each file, decide one of: (a) it is newer work and belongs "
        "upstream in the template as its own change, (b) it is local "
        "divergence that should not survive, and is discarded explicitly with "
        "`tools/runtime_sync.py mirror ... --overwrite-ahead` having first "
        "confirmed by digest what is being discarded, or (c) {name} owns it, "
        "and it is declared in that deployment's `pinned` list so it stops "
        "being compared and is named in every report instead of refused on "
        "every mirror.".format(name=entry.name)
    )


def compare_deployment(template_root, entry):
    """Drift between the template runtime under test and one declared deployment.

    The comparison is `runtime_sync.compare` rather than anything written here,
    so what this test fails on is exactly what the mirror repairs. A second
    implementation of "level" would be this module's own defect class — an
    uninstrumented copy path — turned on the check meant to catch it.
    """
    return runtime_sync.compare(
        runtime_sync.describe_copy(template_root),
        entry.as_copy(),
    )


class DeploymentDriftCase(unittest.TestCase):
    """Base for the per-deployment cases. Never collected on its own.

    Subclasses set ``entry`` and ``template_root``; :func:`build_case` makes
    them. The base carries no ``entry``, so a bare collection of this class
    would error rather than silently pass — which is why it is not named
    ``*Tests`` and is excluded from collection by that convention.
    """

    #: pytest collects every `unittest.TestCase` subclass it imports, whatever
    #: the class is called, so the base has to opt out by name. `build_case`
    #: opts each generated subclass back in. Without this the base would run
    #: with no `entry` and fail on every machine.
    __test__ = False

    entry = None
    template_root = None

    def setUp(self):
        if not self.entry.root.exists():
            checkout_layout.skip_visibly(
                "the deployment {name} is declared in {registry} at {root}, and "
                "no such directory exists on this machine, so there is nothing "
                "to compare; a deployment that is not installed here is not "
                "drift".format(
                    name=self.entry.name,
                    registry=self.registry_path,
                    root=self.entry.root,
                )
            )
        if not self.entry.root.is_dir():
            self.fail(
                "the deployment {name} is declared at {root}, which exists and "
                "is not a directory".format(
                    name=self.entry.name, root=self.entry.root
                )
            )
        self.report = compare_deployment(self.template_root, self.entry)

    def _header(self):
        return (
            "{name} ({root}) has drifted from the template runtime "
            "({template}).".format(
                name=self.entry.name,
                root=self.entry.root,
                template=self.template_root,
            )
        )

    def _held_out(self):
        if not self.report.declared_excluded:
            return ""
        return (
            "\n  {name} declares it owns, and this comparison held out: "
            "{paths}".format(
                name=self.entry.name,
                paths=", ".join(self.report.declared_excluded),
            )
        )

    def test_no_runtime_file_is_absent_from_either_copy(self):
        """A file in one copy and not the other is a deletion, not an edit."""
        if not self.report.missing_files:
            return
        detail = []
        if self.report.absent_from_destination:
            detail.append(
                "  present in the template and absent from {name} — {name} is "
                "missing runtime, which mirroring restores:\n{files}\n"
                "  Repair: {command}".format(
                    name=self.entry.name,
                    files="\n".join(
                        "    " + rel for rel in self.report.absent_from_destination
                    ),
                    command=mirror_command(self.template_root, self.entry),
                )
            )
        if self.report.absent_from_source:
            detail.append(
                "  present in {name} and absent from the template — this file "
                "exists in one copy only:\n{files}\n  {advice}".format(
                    name=self.entry.name,
                    files="\n".join(
                        "    " + rel for rel in self.report.absent_from_source
                    ),
                    advice=deployment_ahead_advice(self.entry),
                )
            )
        self.fail(
            "{header}\n  {n} file(s) exist in one copy and not the other. That "
            "is a deletion, not an edit.\n{detail}{held}".format(
                header=self._header(),
                n=len(self.report.missing_files),
                detail="\n".join(detail),
                held=self._held_out(),
            )
        )

    def test_the_deployment_is_not_behind_the_template(self):
        """Template-ahead: the deployment needs mirroring, and that is all."""
        behind = self.report.source_ahead
        if not behind:
            return
        self.fail(
            "{header}\n  {n} file(s) where the template is ahead, so {name} is "
            "running older runtime:\n{files}\n  Repair: {command}{held}".format(
                header=self._header(),
                n=len(behind),
                name=self.entry.name,
                files="\n".join(
                    "    " + item.describe("template", self.entry.name)
                    for item in behind
                ),
                command=mirror_command(self.template_root, self.entry),
                held=self._held_out(),
            )
        )

    def test_the_deployment_is_not_ahead_of_the_template(self):
        """Deployment-ahead: a human judgement, and no command repairs it."""
        ahead = self.report.destination_ahead
        undetermined = self.report.undetermined_direction
        if not (ahead or undetermined):
            return
        lines = []
        if ahead:
            lines.append(
                "  {n} file(s) where {name} holds more than the template "
                "does:\n{files}".format(
                    n=len(ahead),
                    name=self.entry.name,
                    files="\n".join(
                        "    " + item.describe("template", self.entry.name)
                        for item in ahead
                    ),
                )
            )
        if undetermined:
            lines.append(
                "  {n} file(s) that differ with equal line counts, so neither "
                "copy is shown to be ahead and neither may be assumed "
                "newer:\n{files}".format(
                    n=len(undetermined),
                    files="\n".join(
                        "    " + item.relative_path for item in undetermined
                    ),
                )
            )
        self.fail(
            "{header}\n{lines}\n  {advice}{held}".format(
                header=self._header(),
                lines="\n".join(lines),
                advice=deployment_ahead_advice(self.entry),
                held=self._held_out(),
            )
        )


def build_case(entry, template_root, registry_path):
    """A TestCase class checking one declared deployment.

    Built per entry rather than looped inside one test so that each deployment
    reports its own outcome: one absent and one drifted must be able to skip and
    fail respectively, which a single case iterating a list cannot express.
    """
    return type(
        "DeploymentParity_{name}Tests".format(
            name="".join(ch if ch.isalnum() else "_" for ch in entry.name)
        ),
        (DeploymentDriftCase,),
        {
            "__test__": True,
            "entry": entry,
            "template_root": pathlib.Path(template_root),
            "registry_path": registry_path,
            "__doc__": "Drift between the template runtime and {name} at "
                       "{root}.".format(name=entry.name, root=entry.root),
        },
    )


def build_absent_registry_case(searched, provenance):
    """A TestCase that skips, naming every path a registry was looked for at.

    A machine with no registry is the ordinary case, and it must be *visibly*
    ordinary. Generating nothing at all would leave the deployment check absent
    from the run rather than known not to be running, which is the distinction
    `skip_visibly` exists to draw.
    """
    paths = ", ".join(str(path) for path in searched) or "(nowhere: no repository)"

    class NoDeploymentRegistryTests(unittest.TestCase):
        """No deployment registry on this machine, so no deployment is watched."""

        def test_a_deployment_registry_would_be_read_from_a_known_path(self):
            checkout_layout.skip_visibly(
                "no deployment registry was found, so no deployed instance is "
                "declared and none is checked; looked for it at {paths}. "
                "Declare deployments there, or point {env} at a registry, to "
                "have their drift reported. {provenance}".format(
                    paths=paths,
                    env=runtime_sync.DEPLOYMENT_REGISTRY_ENV,
                    provenance=provenance,
                )
            )

    return NoDeploymentRegistryTests


def build_unreadable_registry_case(path, error):
    """A TestCase that fails, because a malformed registry watches nothing."""

    class UnreadableDeploymentRegistryTests(unittest.TestCase):
        """A registry exists and cannot be read, which is never a skip."""

        def test_the_deployment_registry_parses(self):
            self.fail(
                "the deployment registry {path} exists and could not be read, "
                "so every deployment it declares is going unwatched: {error}"
                .format(path=path, error=error)
            )

    return UnreadableDeploymentRegistryTests


def _install(namespace):
    """Generate this machine's cases into ``namespace``.

    Kept a function so the proof tests can build the same classes against a
    temporary world without the ambient registry taking part.
    """
    checkout = checkout_layout.identify_template_checkout(ADWS)
    if checkout is None:
        # A deployed instance is not the place this check runs from: it would be
        # comparing itself against a list of its own siblings. The template
        # checkouts watch the deployments, not the other way round.
        class DeploymentParityNotApplicableTests(unittest.TestCase):
            """This runtime is a deployed instance, not a template checkout."""

            def test_the_deployment_check_runs_from_a_template_checkout(self):
                checkout_layout.skip_visibly(
                    "this ADW runtime at {root} is a deployed instance rather "
                    "than a template checkout, so it does not watch "
                    "deployments; the check runs in the maestro and "
                    "the-library repositories".format(root=ADWS)
                )

        namespace["DeploymentParityNotApplicableTests"] = (
            DeploymentParityNotApplicableTests
        )
        return

    try:
        resolution = checkout_layout.resolve_deployment_registry(checkout)
    except runtime_sync.RegistryError as error:
        namespace["UnreadableDeploymentRegistryTests"] = (
            build_unreadable_registry_case("the deployment registry", error)
        )
        return

    if resolution.entries is None or not resolution.entries:
        searched = resolution.searched
        provenance = checkout.provenance
        if resolution.entries is not None:
            # A registry that is present and declares nothing is the operator
            # having emptied the list, not a machine without one. Say which.
            provenance = (
                "the registry {path} was read and declares no deployments. "
                "{provenance}".format(path=resolution.path, provenance=provenance)
            )
            searched = (resolution.path,)
        namespace["NoDeploymentRegistryTests"] = build_absent_registry_case(
            searched, provenance
        )
        return

    for entry in resolution.entries:
        case = build_case(entry, checkout.adws_root, resolution.path)
        namespace[case.__name__] = case


_install(globals())


# --------------------------------------------------------------------------
# Proof that the check above does what it claims.
#
# Each of these builds a real temporary checkout on disk and runs the generated
# TestCase against it, reading the outcome out of a `unittest.TestResult`. The
# check is exercised rather than described: a test that asserted on a mock of
# `compare` would pass just as happily against a check that never compared
# anything, which is the failure mode this whole module exists to remove.
# --------------------------------------------------------------------------


def _run_case(case_class):
    """Run every test in ``case_class`` and hand back the result object."""
    suite = unittest.TestLoader().loadTestsFromTestCase(case_class)
    result = unittest.TestResult()
    with warnings.catch_warnings():
        # `skip_visibly` warns on purpose; that is asserted directly elsewhere,
        # and a strict filter in the outer run would otherwise raise it here.
        warnings.simplefilter("always")
        suite.run(result)
    return result


def _outcomes(result):
    """{test method name: (outcome, message)} for a finished run.

    A test that passed does not appear, so an empty mapping is "nothing
    reported anything", which is what a level deployment must produce.
    """
    found = {}
    for test, message in result.failures + result.errors:
        found[test._testMethodName] = ("fail", message)
    for test, message in result.skipped:
        found[test._testMethodName] = ("skip", message)
    return found


class DeploymentCheckProofTests(unittest.TestCase):
    """The four properties the deployment check is worth having for."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.world = pathlib.Path(self._tmp.name)
        self.template = self.world / "maestro" / ".claude/skills/sssf/templates/adws"
        self.deployment = self.world / "lexgenius" / "adws"
        for root in (self.template, self.deployment):
            root.mkdir(parents=True)
        self.registry = self.world / "maestro" / ".maestro" / "deployments.json"
        self.registry.parent.mkdir(parents=True, exist_ok=True)

    def write(self, root, relative, body):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        return path

    def entry(self, name="lexgenius", root=None, pinned=()):
        return runtime_sync.DeploymentEntry(
            name=name,
            root=pathlib.Path(root if root is not None else self.deployment),
            pinned=tuple(pinned),
        )

    def run_check(self, entry=None):
        return _outcomes(
            _run_case(
                build_case(
                    entry if entry is not None else self.entry(),
                    self.template,
                    self.registry,
                )
            )
        )

    # -- present and drifted --------------------------------------------

    def test_a_present_deployment_that_is_behind_the_template_fails(self):
        """The `code_review.py` shape: the deployment is running older runtime."""
        self.write(self.template, "adw_modules/code_review.py", "a\nb\nc\nd\n")
        self.write(self.deployment, "adw_modules/code_review.py", "a\n")

        outcomes = self.run_check()

        outcome, message = outcomes["test_the_deployment_is_not_behind_the_template"]
        self.assertEqual("fail", outcome)
        self.assertIn("adw_modules/code_review.py", message)
        self.assertIn("template is ahead by 3 lines", message)
        self.assertIn("runtime_sync.py", message)
        self.assertNotIn(
            "test_the_deployment_is_not_ahead_of_the_template", outcomes,
            "a deployment that is only behind must not also report as ahead",
        )

    def test_a_present_deployment_missing_a_runtime_file_fails_on_absence(self):
        """The 6,009-line shape: absence is never reported as a difference."""
        self.write(self.template, "adw_modules/deleted.py", "runtime\n")

        outcomes = self.run_check()

        outcome, message = outcomes["test_no_runtime_file_is_absent_from_either_copy"]
        self.assertEqual("fail", outcome)
        self.assertIn("adw_modules/deleted.py", message)
        self.assertIn("deletion, not an edit", message)
        self.assertNotIn(
            "test_the_deployment_is_not_behind_the_template", outcomes,
            "an absent file is not a content difference and must not be counted "
            "as one",
        )

    def test_a_level_deployment_passes_every_check(self):
        """The positive control: nothing fails when the copies agree."""
        for root in (self.template, self.deployment):
            self.write(root, "adw_modules/shared.py", "runtime\n")
            self.write(root, "maestro.py", "more runtime\n")
        self.write(self.deployment, "maestro.config.yaml", "lanes: this-one\n")
        self.write(self.template, "maestro.config.yaml", "lanes: template\n")

        self.assertEqual({}, self.run_check())

    # -- absent ----------------------------------------------------------

    def test_a_declared_deployment_that_is_not_on_this_machine_skips(self):
        missing = self.world / "not-installed-here" / "adws"
        self.write(self.template, "maestro.py", "runtime\n")

        outcomes = self.run_check(self.entry(name="absent", root=missing))

        self.assertEqual(3, len(outcomes), "every check on an absent deployment skips")
        for outcome, message in outcomes.values():
            self.assertEqual("skip", outcome)
            self.assertIn(str(missing), message)
            self.assertIn(str(self.registry), message)

    def test_the_absent_skip_is_warned_so_it_shows_in_a_default_run(self):
        missing = self.world / "not-installed-here" / "adws"
        case = build_case(
            self.entry(name="absent", root=missing), self.template, self.registry
        )
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            _run_case(case)
        reasons = [
            str(item.message)
            for item in caught
            if issubclass(item.category, checkout_layout.PeerCheckoutMissing)
        ]
        self.assertTrue(reasons, "a skip nobody sees is the failure being avoided")
        self.assertIn(str(missing), reasons[0])

    def test_no_registry_at_all_skips_naming_every_path_it_looked_at(self):
        searched = (
            self.world / "maestro" / ".maestro" / "deployments.json",
            self.world / "worktree" / ".maestro" / "deployments.json",
        )
        outcomes = _outcomes(
            _run_case(build_absent_registry_case(searched, "provenance detail"))
        )

        (outcome, message), = outcomes.values()
        self.assertEqual("skip", outcome)
        for path in searched:
            self.assertIn(str(path), message)
        self.assertIn(runtime_sync.DEPLOYMENT_REGISTRY_ENV, message)

    def test_a_registry_that_exists_and_cannot_be_read_fails_rather_than_skips(self):
        outcomes = _outcomes(
            _run_case(
                build_unreadable_registry_case(self.registry, "REGISTRY_NOT_JSON")
            )
        )

        (outcome, message), = outcomes.values()
        self.assertEqual("fail", outcome)
        self.assertIn("going unwatched", message)

    # -- pinned ----------------------------------------------------------

    def test_a_pinned_path_does_not_fail_the_check(self):
        """Issue #71 option (c): the deployment owns this file outright."""
        self.write(self.template, "adw_modules/deliver.py", "template\nversion\n")
        self.write(self.deployment, "adw_modules/deliver.py", "deployment\n")
        self.write(self.template, "adw_modules/only_upstream.py", "runtime\n")
        self.write(self.deployment, "adw_modules/only_upstream.py", "runtime\n")

        pinned_outcomes = self.run_check(
            self.entry(pinned=("adw_modules/deliver.py",))
        )

        self.assertEqual({}, pinned_outcomes)
        # and the same tree without the pin is a failure, so the pin is what
        # made the difference rather than the file being uncompared anyway.
        self.assertIn(
            "test_the_deployment_is_not_behind_the_template", self.run_check()
        )

    def test_a_pinned_path_is_named_in_the_report_rather_than_silently_dropped(self):
        self.write(self.template, "adw_modules/deliver.py", "template\nversion\n")
        self.write(self.deployment, "adw_modules/deliver.py", "deployment\n")
        # something else drifts, so the report is printed at all
        self.write(self.template, "maestro.py", "a\nb\n")
        self.write(self.deployment, "maestro.py", "a\n")
        entry = self.entry(pinned=("adw_modules/deliver.py",))

        outcomes = self.run_check(entry)
        _, message = outcomes["test_the_deployment_is_not_behind_the_template"]

        self.assertIn("declares it owns", message)
        self.assertIn("adw_modules/deliver.py", message)

        report = compare_deployment(self.template, entry)
        self.assertEqual(("adw_modules/deliver.py",), report.declared_excluded)
        self.assertIn("adw_modules/deliver.py", report.excluded)
        self.assertIn("adw_modules/deliver.py", report.describe())

    def test_a_level_deployment_still_names_what_it_held_out(self):
        """The quiet case: nothing failed, and the exclusion is still visible."""
        self.write(self.template, "adw_modules/deliver.py", "template\nversion\n")
        self.write(self.deployment, "adw_modules/deliver.py", "deployment\n")
        entry = self.entry(pinned=("adw_modules/deliver.py",))

        report = compare_deployment(self.template, entry)

        self.assertTrue(report.is_level)
        self.assertIn("are level", report.describe())
        self.assertIn("adw_modules/deliver.py", report.describe())

    # -- direction -------------------------------------------------------

    def test_deployment_ahead_is_a_different_finding_from_template_ahead(self):
        """The two directions fail different tests and offer different repairs."""
        self.write(self.template, "adw_modules/behind.py", "a\nb\nc\n")
        self.write(self.deployment, "adw_modules/behind.py", "a\n")
        self.write(self.template, "adw_modules/ahead.py", "a\n")
        self.write(self.deployment, "adw_modules/ahead.py", "a\nb\nc\n")

        outcomes = self.run_check()

        behind_outcome, behind = outcomes[
            "test_the_deployment_is_not_behind_the_template"
        ]
        ahead_outcome, ahead = outcomes[
            "test_the_deployment_is_not_ahead_of_the_template"
        ]
        self.assertEqual(("fail", "fail"), (behind_outcome, ahead_outcome))

        self.assertIn("adw_modules/behind.py", behind)
        self.assertNotIn("adw_modules/ahead.py", behind)
        self.assertIn("Repair:", behind)

        self.assertIn("adw_modules/ahead.py", ahead)
        self.assertNotIn("adw_modules/behind.py", ahead)
        self.assertIn("no command is offered here", ahead)
        self.assertNotIn("Repair:", ahead)

    def test_an_equal_length_difference_is_reported_where_a_human_will_read_it(self):
        """Line count says nothing here, so nothing may be assumed newer."""
        self.write(self.template, "adw_modules/same_length.py", "a\nb\n")
        self.write(self.deployment, "adw_modules/same_length.py", "a\nc\n")

        outcomes = self.run_check()

        self.assertNotIn("test_the_deployment_is_not_behind_the_template", outcomes)
        outcome, message = outcomes[
            "test_the_deployment_is_not_ahead_of_the_template"
        ]
        self.assertEqual("fail", outcome)
        self.assertIn("neither copy is shown to be ahead", message)
        self.assertIn("adw_modules/same_length.py", message)

    def test_a_deployment_only_file_is_reported_as_work_in_one_copy_only(self):
        self.write(self.deployment, "adw_modules/local.py", "only here\n")

        outcome, message = self.run_check()[
            "test_no_runtime_file_is_absent_from_either_copy"
        ]

        self.assertEqual("fail", outcome)
        self.assertIn("absent from the template", message)
        self.assertIn("adw_modules/local.py", message)
        self.assertIn("no command is offered here", message)

    # -- the deployment's own config never counts as drift ----------------

    def test_the_deployments_own_maestro_config_is_never_drift(self):
        self.write(self.template, "maestro.config.yaml", "lanes: template\n")
        self.write(self.deployment, "maestro.config.yaml", "lanes: this-one\n")

        self.assertEqual({}, self.run_check())

    def test_a_deployment_installed_at_a_template_shaped_path_is_still_a_deployment(
        self,
    ):
        """Classification by path shape must not be the last word here."""
        odd = self.world / "somewhere" / ".claude/skills/sssf/templates/adws"
        odd.mkdir(parents=True)
        self.write(odd, "maestro.config.yaml", "lanes: this-one\n")
        self.write(self.template, "maestro.config.yaml", "lanes: template\n")

        entry = self.entry(name="odd", root=odd)

        self.assertEqual(runtime_sync.DEPLOYMENT, entry.as_copy().kind)
        self.assertEqual({}, self.run_check(entry))


if __name__ == "__main__":
    unittest.main()
