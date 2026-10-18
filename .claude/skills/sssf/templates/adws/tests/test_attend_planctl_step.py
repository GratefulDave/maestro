"""The approval step `run attend` performs between authoring and amending.

An attended revision is not applied because an agent wrote it. It is applied
because `planctl` validated it, a receipt approved it, and the ingress
projection accepted that receipt -- the same four steps the human ran by hand
on an earlier run. These cases pin the parts of that step Maestro owns:
where the validator and the reviewer key are resolved from, what happens when
either is absent, that a refusing planctl becomes a typed refusal carrying its
own diagnostic, that the receipt is signed as the independent reviewer agent
and never as Maestro, and that the receipt shape Maestro mints is the shape
ingress verifies.

`_run_planctl` is exercised against the real binary when one is configured on
this machine. A stubbed `subprocess.run` cannot observe an argv parser: it
records the argv you passed and replays the stdout you scripted, which is
exactly how `vitest list --json <paths>` shipped.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

import maestro
from adw_modules import attend as att
from adw_modules import plan_contract_ingress as ingress
from adw_modules import route_admission as admission


#: Where this machine's plan-contract validator lives, if anywhere. Read from
#: the environment rather than pinned: the path is a worktree of another
#: repository and is nobody's invariant.
_PLANCTL_ENV = "MAESTRO_TEST_PLANCTL"


def _configured_planctl() -> Path | None:
    raw = os.environ.get(_PLANCTL_ENV, "")
    if not raw:
        return None
    candidate = Path(raw)
    return candidate if candidate.is_file() else None


def _forbid_aws(case: unittest.TestCase, runner: str = "/nonexistent") -> None:
    """Point planctl's default mutation runner somewhere that cannot reach AWS."""
    from unittest import mock

    patcher = mock.patch.dict(os.environ, {"PLANCTL_DEFAULT_RUNNER": runner})
    patcher.start()
    case.addCleanup(patcher.stop)


class _Runtime:
    def __init__(self, path: Path) -> None:
        self.path = path


class ValidatorResolution(unittest.TestCase):
    def setUp(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.tmp = Path(holder.name)

    def test_an_unconfigured_validator_refuses_by_name(self) -> None:
        with self.assertRaises(att.AttendRefused) as caught:
            maestro._planctl_binary(att.AttendPolicy(max_amendments_per_lane=1))
        self.assertEqual(caught.exception.code, att.PLANCTL_UNRESOLVED)

    def test_a_configured_validator_that_is_not_there_refuses_by_name(self) -> None:
        policy = att.AttendPolicy(
            max_amendments_per_lane=1, planctl=self.tmp / "absent" / "planctl.py"
        )
        with self.assertRaises(att.AttendRefused) as caught:
            maestro._planctl_binary(policy)
        self.assertEqual(caught.exception.code, att.PLANCTL_UNRESOLVED)
        self.assertIn("planctl.py", caught.exception.detail)


class ReviewerKeyResolution(unittest.TestCase):
    """One resolver: `keys_dir` from the deployment config, else the state root's `keys/`."""

    def setUp(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.tmp = Path(holder.name)
        self.state = self.tmp / "state"
        self.state.mkdir()

    def _layout(self, **extra: str) -> dict:
        from tests import plan_receipts

        config = plan_receipts.write_deployment_config(self.tmp / "adws", self.state, **extra)
        return maestro._load_maestro_config(self.tmp, config)

    def test_without_keys_dir_the_key_comes_from_the_state_root(self) -> None:
        keys = self.state / "keys"
        keys.mkdir()
        material = "ab" * 32
        (keys / admission.REVIEWER_HMAC_KEY_FILE).write_text(material)
        self.assertEqual(maestro._reviewer_hmac_key(self._layout()), material)
        # planctl's own floor. A key shorter than this mints no receipt.
        self.assertGreaterEqual(len(material.encode("utf-8")), 32)

    def test_keys_dir_outside_the_state_root_is_where_the_key_is_read(self) -> None:
        """FDAdb: state root moved, keys stayed in ~/.maestro/FDAdb/keys."""
        keys = self.tmp / "dot-maestro" / "FDAdb" / "keys"
        keys.mkdir(parents=True)
        (keys / admission.REVIEWER_HMAC_KEY_FILE).write_text("cd" * 32 + "\n")
        layout = self._layout(keys_dir=str(keys))
        self.assertFalse((self.state / "keys").exists())
        self.assertEqual(keys, layout["keys_dir"])
        self.assertEqual("cd" * 32, maestro._reviewer_hmac_key(layout))

    def test_a_relative_keys_dir_is_a_configuration_error(self) -> None:
        with self.assertRaises(maestro._MaestroConfigurationError):
            self._layout(keys_dir="keys")

    def test_an_absent_key_refuses_by_name(self) -> None:
        with self.assertRaises(att.AttendRefused) as caught:
            maestro._reviewer_hmac_key(self._layout())
        self.assertEqual(caught.exception.code, att.KEY_UNRESOLVED)

    def test_an_empty_key_file_is_not_a_key(self) -> None:
        keys = self.state / "keys"
        keys.mkdir()
        (keys / admission.REVIEWER_HMAC_KEY_FILE).write_text("   \n")
        with self.assertRaises(att.AttendRefused) as caught:
            maestro._reviewer_hmac_key(self._layout())
        self.assertEqual(caught.exception.code, att.KEY_UNRESOLVED)


class ReceiptHandshake(unittest.TestCase):
    """What ingress requires of a receipt, pinned against ingress itself."""

    def setUp(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.tmp = Path(holder.name)
        self.ir_bytes = json.dumps({"plan_id": "p-1"}, sort_keys=True).encode("utf-8")

    def _receipt(self, *, drop: tuple = (), **overrides: object) -> dict:
        from tests import plan_receipts

        receipt = plan_receipts.signed_receipt(self.ir_bytes, **overrides)
        for name in drop:
            receipt.pop(name)
        receipt["signature"] = plan_receipts.plan_approval.signature(
            receipt, plan_receipts.KEY)
        return receipt

    def _verify(self, receipt: dict) -> None:
        from tests import plan_receipts

        ingress._verify_receipt(self.ir_bytes, receipt, None, plan_receipts.KEY)

    def test_a_pass_receipt_over_these_exact_bytes_verifies(self) -> None:
        self._verify(self._receipt())

    def test_a_receipt_for_an_earlier_revision_is_refused(self) -> None:
        stale = self._receipt(ir_sha256=hashlib.sha256(b"older").hexdigest())
        with self.assertRaises(ingress.IngressError) as caught:
            self._verify(stale)
        self.assertIn("RECEIPT_IR_MISMATCH", str(caught.exception))

    def test_a_receipt_not_signed_with_findings_is_refused(self) -> None:
        for field in ("findings_sha256", "question_surface_sha256"):
            with self.subTest(field=field):
                with self.assertRaises(ingress.IngressError) as caught:
                    self._verify(self._receipt(drop=(field,)))
                self.assertIn("RECEIPT_WITHOUT_FINDINGS", str(caught.exception))

    def test_a_receipt_that_is_not_a_pass_is_refused(self) -> None:
        with self.assertRaises(ingress.IngressError) as caught:
            self._verify(self._receipt(verdict="REVISE"))
        self.assertIn("RECEIPT_NOT_PASS", str(caught.exception))

    def test_a_receipt_without_a_valid_signature_is_refused(self) -> None:
        genuine = self._receipt()
        for name, forged, code in (
            ("unsigned", {k: v for k, v in genuine.items() if k != "signature"}, "RECEIPT_SIGNATURE"),
            ("edited", dict(genuine, findings_sha256="9" * 64), "RECEIPT_SIGNATURE"),
            ("other key id", dict(genuine, reviewer_key_id="0" * 64), "RECEIPT_KEY_ID"),
            ("algorithm", dict(genuine, signature_algorithm="none"), "RECEIPT_SIGNATURE_ALGORITHM"),
        ):
            with self.subTest(forgery=name):
                with self.assertRaises(ingress.IngressError) as caught:
                    self._verify(forged)
                self.assertIn(code, str(caught.exception))


_REVIEWER = att.AttendPolicy(
    max_amendments_per_lane=1,
    reviewer_id="attend-reviewer:omp:profile-b",
    reviewer_vendor="vendor-b",
)


class ReviewSignsTheIndependentReviewersFindings(unittest.TestCase):
    """`planctl review --findings` signs the reviewer agent's answer, as it.

    The author no longer answers its own review: `_attend_project` takes the
    findings the attend reviewer wrote, binds them to the revision and to the
    reviewer's identity, and names that identity on the review argv.
    """

    def setUp(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.tmp = Path(holder.name)
        self.revision = self.tmp / "out" / "r2.ir.json"
        self.revision.parent.mkdir()
        self.revision.write_text("{}", encoding="utf-8")
        self.plans = self.tmp / "plans"

    def _review_argv(self, policy: att.AttendPolicy, findings: list) -> list:
        from unittest import mock

        calls = []

        def run_planctl(binary, argv, **kwargs):
            calls.append(argv)
            if argv[0] == "question-surface":
                return json.dumps({"plan_id": "p-1", "question_surface_sha256": "a" * 64})
            return ""

        with (
            mock.patch.object(maestro, "_planctl_binary", return_value=Path("planctl")),
            mock.patch.object(maestro, "_reviewer_hmac_key", return_value="k" * 32),
            mock.patch.object(maestro, "_run_planctl", side_effect=run_planctl),
            mock.patch.object(
                ingress, "author_from_plan_contract", side_effect=RuntimeError("stop")
            ),
        ):
            with self.assertRaises(att.AttendRefused):
                maestro._attend_project(
                    policy, {"keys_dir": self.tmp / "keys"}, self.tmp, self.plans,
                    "run", self.revision.read_bytes(), 2, findings,
                )
        (review,) = [argv for argv in calls if argv[0] == "review"]
        return review

    def test_the_reviewer_agents_findings_are_bound_and_signed_as_it(self) -> None:
        answer = [{"finding_id": "f-1", "claim_id": "claim-a"}]
        review = self._review_argv(_REVIEWER, answer)
        passed = Path(review[review.index("--findings") + 1])
        self.assertEqual(self.plans / "run.r2.review-findings.json", passed)
        self.assertEqual(
            review[review.index("--reviewer") + 1], "attend-reviewer:omp:profile-b"
        )
        self.assertEqual(review[review.index("--reviewer-vendor") + 1], "vendor-b")
        self.assertNotIn("maestro-attend", review)
        self.assertEqual(
            {
                "schema_version": "plan-contract-review-findings.v2",
                "plan_id": "p-1",
                "question_surface_sha256": "a" * 64,
                "reviewer_id": "attend-reviewer:omp:profile-b",
                "reviewer_vendor": "vendor-b",
                "findings": answer,
            },
            json.loads(passed.read_text(encoding="utf-8")),
        )

    def test_a_file_the_author_left_beside_its_revision_is_never_read(self) -> None:
        """The removed path: an author-written findings file signs nothing."""
        (self.revision.parent / "r2.review-findings.json").write_text(
            json.dumps({"findings": [{"finding_id": "author-wrote-this"}]}),
            encoding="utf-8",
        )
        review = self._review_argv(_REVIEWER, [])
        passed = json.loads(
            Path(review[review.index("--findings") + 1]).read_text(encoding="utf-8")
        )
        self.assertEqual(passed["findings"], [])

    def test_no_reviewer_identity_refuses_before_any_planctl_call(self) -> None:
        from unittest import mock

        with mock.patch.object(maestro, "_run_planctl") as planctl:
            with self.assertRaises(att.AttendRefused) as caught:
                maestro._attend_project(
                    att.AttendPolicy(max_amendments_per_lane=1),
                    {"keys_dir": self.tmp / "keys"}, self.tmp, self.plans,
                    "run", self.revision.read_bytes(), 2, [],
                )
        self.assertEqual(caught.exception.code, att.REVIEWER_UNCONFIGURED)
        planctl.assert_not_called()

    def test_validate_is_given_the_key_review_signed_with(self) -> None:
        from unittest import mock

        keyed = {}

        def run_planctl(binary, argv, **kwargs):
            keyed[argv[0]] = kwargs.get("key")
            if argv[0] == "question-surface":
                return json.dumps({"plan_id": "p-1", "question_surface_sha256": "a" * 64})
            return ""

        with (
            mock.patch.object(maestro, "_planctl_binary", return_value=Path("planctl")),
            mock.patch.object(maestro, "_reviewer_hmac_key", return_value="k" * 32),
            mock.patch.object(maestro, "_run_planctl", side_effect=run_planctl),
            mock.patch.object(
                ingress, "author_from_plan_contract", side_effect=RuntimeError("stop")
            ),
        ):
            with self.assertRaises(att.AttendRefused):
                maestro._attend_project(
                    _REVIEWER, {"keys_dir": self.tmp / "keys"}, self.tmp,
                    self.plans, "run", self.revision.read_bytes(), 2, [],
                )
        self.assertEqual(keyed["review"], "k" * 32)
        self.assertEqual(keyed["validate"], "k" * 32)

    def test_the_receipt_path_is_maestros_not_the_authors(self) -> None:
        """F1: the IR planctl reviews names the receipt Maestro writes."""
        self.revision.write_text(
            json.dumps({"approval": {"required": True, "receipt_path": "old.receipt.json"}}),
            encoding="utf-8",
        )
        review = self._review_argv(_REVIEWER, [])
        written = json.loads((self.plans / "run.r2.ir.json").read_text())
        self.assertEqual(
            written["approval"], {"required": True, "receipt_path": "run.r2.receipt.json"}
        )
        receipt_out = Path(review[review.index("--receipt-out") + 1])
        self.assertEqual(receipt_out.name, written["approval"]["receipt_path"])
        self.assertEqual(receipt_out.parent, self.plans)

    def test_a_revision_that_is_not_a_json_object_is_refused(self) -> None:
        from unittest import mock

        for raw in (b"not json", b"[1, 2]"):
            with self.subTest(raw=raw):
                with (
                    mock.patch.object(maestro, "_planctl_binary", return_value=Path("p")),
                    mock.patch.object(maestro, "_reviewer_hmac_key", return_value="k" * 32),
                    mock.patch.object(maestro, "_run_planctl") as planctl,
                ):
                    with self.assertRaises(att.AttendRefused) as caught:
                        maestro._attend_project(
                            _REVIEWER, {"keys_dir": self.tmp / "keys"}, self.tmp,
                            self.plans, "run", raw, 2, [],
                        )
                self.assertEqual(caught.exception.code, att.REVISION_REFUSED)
                planctl.assert_not_called()

    def test_two_sessions_never_share_a_round_file(self) -> None:
        """F9: same run, same revision, same round, different sessions."""
        from unittest import mock

        compiled = mock.Mock(plan_revision=1, lanes=())
        operator_cwd = self.tmp / "operator"
        reviewer_cwd = self.tmp / "reviewer"
        paths = {}
        for session, content in (("session-a", b'{"a": 1}'), ("session-b", b'{"b": 2}')):
            request = maestro._attend_request(
                None, None, {}, "run", (), compiled, self.revision, operator_cwd,
                1, (), "", session_id=session,
            )
            out = Path(request.revision_out_path)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(content)
            with (
                mock.patch.object(maestro, "_planctl_binary", return_value=Path("p")),
                mock.patch.object(maestro, "_run_planctl", return_value="{}"),
            ):
                review = maestro._attend_review_request(
                    _REVIEWER, request, out, (), 1, att.sha256_hex(content),
                    reviewer_cwd, session_id=session,
                )
            paths[session] = (out, Path(review.revision_ir_path), review.findings_out_path)
            self.assertIn(session, out.name)
        (a_out, a_snap, a_find), (b_out, b_snap, b_find) = paths.values()
        self.assertNotEqual(a_out, b_out)
        self.assertNotEqual(a_snap, b_snap)
        self.assertNotEqual(a_find, b_find)
        # The first session's bytes survive the second session's round.
        self.assertEqual(a_out.read_bytes(), b'{"a": 1}')
        self.assertEqual(a_snap.read_bytes(), b'{"a": 1}')

    def test_the_reviewer_id_is_a_planctl_identifier(self) -> None:
        import re

        for route in (
            {"route": "omp", "profile": "openai astra/x", "model": "", "effort": ""},
            {"route": "claude", "model": "opus", "effort": "medium", "profile": ""},
        ):
            with self.subTest(route=route):
                reviewer = maestro._attend_reviewer_id(route)
                # planctl's ID_RE; `planctl review` refuses anything else.
                self.assertRegex(reviewer, re.compile(r"^[A-Za-z][A-Za-z0-9._:-]*$"))

    def _prompt(self, role: str, extra: dict) -> dict:
        from types import SimpleNamespace

        from adw_modules import scheduler_types as st

        actor = maestro.HerdrStageActor.__new__(maestro.HerdrStageActor)
        actor.lane_specs = {}
        ctx = SimpleNamespace(
            lane=SimpleNamespace(lane_id="lane-a", lane_kind=st.LANE_KIND_BUILD),
            plan_revision=2,
            run_id="run1",
            stage=st.LaneStage.WAITING_FOR_USER,
        )
        return maestro.HerdrStageActor._prompt(
            actor, ctx, role, self.tmp / "envelope.json", self.tmp, extra
        )

    def test_the_reviewer_is_asked_the_question_and_the_author_is_not(self) -> None:
        reviewer = self._prompt("attend-reviewer", {"findings_out_path": "x"})
        operator = self._prompt("operator", {})
        self.assertIn(att.TWO_IMPLEMENTATIONS_QUESTION, reviewer["instructions"])
        self.assertNotIn(att.TWO_IMPLEMENTATIONS_QUESTION, operator["instructions"])
        self.assertNotIn("findings_out_path", operator["instructions"])


class RealValidatorInvocation(unittest.TestCase):
    """`_run_planctl` against the binary, not against a stubbed subprocess."""

    def setUp(self) -> None:
        self.binary = _configured_planctl()
        if self.binary is None:
            self.skipTest(
                "no plan-contract validator on this machine: set {0} to its "
                "path to run this case".format(_PLANCTL_ENV)
            )
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.tmp = Path(holder.name)
        _forbid_aws(self)

    def test_a_refusing_validator_becomes_a_typed_refusal_with_its_own_words(
        self,
    ) -> None:
        ir = self.tmp / "plan.ir.json"
        ir.write_text(json.dumps({"schema_version": 0}), encoding="utf-8")
        with self.assertRaises(att.AttendRefused) as caught:
            maestro._run_planctl(
                self.binary,
                ["validate", str(ir), "--repo-root", str(self.tmp), "--json"],
            )
        self.assertEqual(caught.exception.code, att.REVISION_REFUSED)
        self.assertIn("planctl validate", caught.exception.detail)
        # planctl's own diagnostic, not a sentence Maestro made up. The
        # operator reading a refused attend needs the field that is wrong.
        self.assertIn("schema", caught.exception.detail)

    def test_the_reviewer_key_is_injected_and_never_inherited(self) -> None:
        ir = self.tmp / "plan.ir.json"
        ir.write_text(json.dumps({"schema_version": 0}), encoding="utf-8")
        os.environ[admission.REVIEWER_HMAC_KEY_ENV] = "leaked-from-the-shell"
        self.addCleanup(os.environ.pop, admission.REVIEWER_HMAC_KEY_ENV, None)
        # The call refuses on the IR either way. What is pinned is that the
        # ambient value is dropped: a reviewer key an operator happened to have
        # sourced must not decide which key approved a revision.
        with self.assertRaises(att.AttendRefused):
            maestro._run_planctl(
                self.binary,
                ["validate", str(ir), "--repo-root", str(self.tmp), "--json"],
            )
        self.assertEqual(
            os.environ[admission.REVIEWER_HMAC_KEY_ENV], "leaked-from-the-shell"
        )


    def test_the_binary_accepts_the_findings_argument_attend_passes(self) -> None:
        """A stubbed subprocess cannot observe the argv parser; ask the binary."""
        import subprocess
        import sys

        usage = subprocess.run(
            [sys.executable, str(self.binary), "review", "--help"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(usage.returncode, 0, usage.stderr)
        self.assertIn("--findings", usage.stdout)


def _planctl_fixture(binary: Path, tmp: Path, run_id: str, revision: int) -> tuple:
    """planctl's own valid fixture, placed in a repository of its own.

    The fixture and its schema ship beside the binary, so this IR is one the
    binary itself accepts. Nothing here knows where `_attend_project` writes:
    `approval.receipt_path` keeps the fixture's own value, which names a
    different receipt, so the case fails unless Maestro sets it. The
    fixture's one link points at its own rendered page beside it; a plan's
    links point at repository files, so the link is re-aimed at one, which
    has nothing to do with Maestro's paths.
    """
    import shutil

    del run_id
    skill = binary.parent.parent
    repo = tmp / "repo"
    (repo / "docs").mkdir(parents=True)
    (repo / "docs" / "target.md").write_text("target\n", encoding="utf-8")
    (tmp / "schemas").mkdir()
    shutil.copy(skill / "schemas" / "plan-ir-v1.schema.json", tmp / "schemas")
    ir = json.loads((skill / "fixtures" / "valid-plan-ir.json").read_text())
    ir["links"][0]["href"] = "../repo/docs/target.md"
    ir["links"][0]["canonical_target"] = "docs/target.md"
    authored = tmp / "out" / "r{0}.ir.json".format(revision)
    authored.parent.mkdir()
    authored.write_text(json.dumps(ir), encoding="utf-8")
    return repo, authored


class RealReviewerSigning(unittest.TestCase):
    """The review, validate and receipt steps against the real binary.

    A stubbed planctl accepted `attend-reviewer/omp/x` as a reviewer id and
    a `validate --require-approved` run with no key; the binary refuses
    both. This case runs `_attend_project` unstubbed until ingress, which the
    planctl fixture (it carries no Maestro extension) is expected to refuse.
    """

    def setUp(self) -> None:
        self.binary = _configured_planctl()
        if self.binary is None:
            self.skipTest(
                "no plan-contract validator on this machine: set {0} to its "
                "path to run this case".format(_PLANCTL_ENV)
            )
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.tmp = Path(holder.name)
        _forbid_aws(self)

    def test_the_receipt_is_signed_as_the_reviewer_agent_and_verifies(self) -> None:
        from adw_modules import plan_approval

        repo, authored = _planctl_fixture(self.binary, self.tmp, "run-1", 2)
        self.assertNotEqual(
            json.loads(authored.read_text())["approval"]["receipt_path"],
            "run-1.r2.receipt.json",
        )
        keys = self.tmp / "keys"
        keys.mkdir()
        key = "ab" * 32
        (keys / admission.REVIEWER_HMAC_KEY_FILE).write_text(key)
        route = {"route": "omp", "profile": "profile-b", "model": "", "effort": ""}
        policy = att.AttendPolicy(
            max_amendments_per_lane=1,
            planctl=self.binary,
            reviewer_id=maestro._attend_reviewer_id(route),
            reviewer_vendor="vendor-b",
        )
        plans = repo / "plans"
        with self.assertRaises(att.AttendRefused) as caught:
            maestro._attend_project(
                policy, {"keys_dir": keys}, repo, plans, "run-1", authored.read_bytes(), 2, [],
                local=True,
            )
        # Past render, review and validate: refused only by ingress.
        self.assertEqual(caught.exception.code, att.REVISION_REFUSED)
        self.assertIn("MAESTRO_EXTENSION_MISSING", caught.exception.detail)
        receipt = json.loads((plans / "run-1.r2.receipt.json").read_text())
        self.assertEqual(
            receipt["reviewer"],
            {"id": "attend-reviewer:omp:profile-b", "vendor": "vendor-b"},
        )
        ingress._verify_receipt(
            (plans / "run-1.r2.ir.json").read_bytes(),
            receipt,
            None,
            plan_approval.key_bytes(key),
        )


class ReviewMutationPassLocation(unittest.TestCase):
    """`--local` is forwarded to `planctl review`; EC2 stays the default."""

    def setUp(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.tmp = Path(holder.name)
        self.revision = self.tmp / "r2.ir.json"
        self.revision.write_text("{}", encoding="utf-8")

    def _argvs(self, **kwargs) -> list:
        from unittest import mock

        calls = []

        def run_planctl(binary, argv, **_):
            calls.append(list(argv))
            if argv[0] == "question-surface":
                return json.dumps({"plan_id": "p-1", "question_surface_sha256": "a" * 64})
            return ""

        with (
            mock.patch.object(maestro, "_planctl_binary", return_value=Path("planctl")),
            mock.patch.object(maestro, "_reviewer_hmac_key", return_value="k" * 32),
            mock.patch.object(maestro, "_run_planctl", side_effect=run_planctl),
            mock.patch.object(
                ingress, "author_from_plan_contract", side_effect=RuntimeError("stop")
            ),
        ):
            with self.assertRaises(att.AttendRefused):
                maestro._attend_project(
                    _REVIEWER, {"keys_dir": self.tmp / "keys"}, self.tmp,
                    self.tmp / "plans", "run", self.revision.read_bytes(), 2, [],
                    **kwargs,
                )
        return calls

    def test_the_default_review_argv_has_no_local_flag(self) -> None:
        (review,) = [a for a in self._argvs() if a[0] == "review"]
        self.assertNotIn("--local", review)

    def test_local_is_forwarded_to_review_only(self) -> None:
        calls = self._argvs(local=True)
        (review,) = [a for a in calls if a[0] == "review"]
        self.assertIn("--local", review)
        self.assertEqual(
            [a[0] for a in calls if "--local" in a], ["review"]
        )

    def test_the_parser_exposes_local_on_attend_only(self) -> None:
        parser = maestro.build_parser()
        self.assertFalse(parser.parse_args(["run", "attend", "--run", "r"]).local)
        self.assertTrue(parser.parse_args(["run", "attend", "--run", "r", "--local"]).local)
        with self.assertRaises(SystemExit):
            parser.parse_args(["run", "amend", "plan.json", "--run", "r", "--local"])

    def test_a_remote_runner_failure_surfaces_planctl_words_and_the_hint(self) -> None:
        from unittest import mock

        done = mock.Mock(
            returncode=2,
            stdout="",
            stderr="review.remote_runner: boom; rerun with --local to run the "
            "mutation pass on this machine",
        )
        with mock.patch.object(maestro.subprocess, "run", return_value=done):
            with self.assertRaises(att.AttendRefused) as caught:
                maestro._run_planctl(Path("planctl.py"), ["review", "ir.json"])
        self.assertEqual(caught.exception.code, att.REVISION_REFUSED)
        self.assertIn("rerun with --local", caught.exception.detail)
        self.assertIn("maestro run attend --local", caught.exception.detail)

    def test_other_failures_carry_no_hint(self) -> None:
        from unittest import mock

        done = mock.Mock(returncode=2, stdout="", stderr="receipt.key_missing")
        with mock.patch.object(maestro.subprocess, "run", return_value=done):
            with self.assertRaises(att.AttendRefused) as caught:
                maestro._run_planctl(Path("planctl.py"), ["review", "ir.json"])
        self.assertNotIn("--local", caught.exception.detail)


_FAKE_RUNNER = """#!{python}
import os, subprocess, sys

argv = sys.argv[1:]
out = argv[argv.index("--out") + 1]
with open(os.environ["FAKE_RUNNER_DUMP"], "w") as handle:
    handle.write("invoked")
rest = [a for a in argv if a != "--out" and a != out]
with open(out, "w") as handle:
    subprocess.run(
        [sys.executable, {planctl!r}, "mutate", "--local", *rest[1:], "--workers", "4", "--json"],
        stdout=handle,
        check=False,
    )
"""


class RealReviewThroughAttendProject(unittest.TestCase):
    """`_attend_project` (not the whole verb, which needs a live run) against the
    mutate-remote planctl and a fake EC2 runner."""

    def setUp(self) -> None:
        raw = os.environ.get("MAESTRO_TEST_PLANCTL_REMOTE", "") or os.environ.get(
            _PLANCTL_ENV, ""
        )
        self.binary = Path(raw) if raw and Path(raw).is_file() else None
        if self.binary is None or "--local" not in self.binary.read_text(encoding="utf-8"):
            self.skipTest("set MAESTRO_TEST_PLANCTL to a planctl with the EC2 default")
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.tmp = Path(holder.name)
        self.dump = self.tmp / "runner-dump"
        runner = self.tmp / "fake-runner.py"
        runner.write_text(
            _FAKE_RUNNER.format(python=sys.executable, planctl=str(self.binary)),
            encoding="utf-8",
        )
        runner.chmod(runner.stat().st_mode | stat.S_IXUSR)
        from unittest import mock

        patcher = mock.patch.dict(
            os.environ,
            {"PLANCTL_DEFAULT_RUNNER": str(runner), "FAKE_RUNNER_DUMP": str(self.dump)},
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _project(self, **kwargs) -> att.AttendRefused:
        tmp = self.tmp / "work"
        tmp.mkdir()
        repo, authored = _planctl_fixture(self.binary, tmp, "run-1", 2)
        keys = tmp / "keys"
        keys.mkdir()
        (keys / admission.REVIEWER_HMAC_KEY_FILE).write_text("ab" * 32)
        route = {"route": "omp", "profile": "profile-b", "model": "", "effort": ""}
        policy = att.AttendPolicy(
            max_amendments_per_lane=1,
            planctl=self.binary,
            reviewer_id=maestro._attend_reviewer_id(route),
            reviewer_vendor="vendor-b",
        )
        with self.assertRaises(att.AttendRefused) as caught:
            maestro._attend_project(
                policy, {"keys_dir": keys}, repo, repo / "plans", "run-1",
                authored.read_bytes(), 2, [], **kwargs,
            )
        return caught.exception

    def test_without_local_the_runner_is_invoked(self) -> None:
        refused = self._project()
        self.assertTrue(self.dump.exists())
        # Past review: only ingress refuses the planctl fixture.
        self.assertIn("MAESTRO_EXTENSION_MISSING", refused.detail)

    def test_with_local_the_runner_is_not_invoked(self) -> None:
        refused = self._project(local=True)
        self.assertFalse(self.dump.exists())
        self.assertIn("MAESTRO_EXTENSION_MISSING", refused.detail)

    def test_an_unrunnable_runner_names_local_and_never_falls_back(self) -> None:
        from unittest import mock

        with mock.patch.dict(os.environ, {"PLANCTL_DEFAULT_RUNNER": "/nonexistent"}):
            refused = self._project()
        self.assertFalse(self.dump.exists())
        self.assertIn("review.remote_runner", refused.detail)
        self.assertIn("--local", refused.detail)
        self.assertIn("maestro run attend --local", refused.detail)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
