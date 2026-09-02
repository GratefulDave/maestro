"""The sealed probe, measured against the real runners rather than a stub.

`tests/test_sealed_probe.py` stubs `_run_sealed_suite` on purpose: it pins the
overlay, the scratch-tree lifetime, and the redaction wiring, and none of those
need a runner. What a stub cannot do is observe a runner. It replays the stdout
the test scripted, so it agrees with whatever the test already believed --
which is how `vitest list --json <paths>` shipped, reading its own argument as
a destination and overwriting the file it was measuring
(`.claude/rules/lane_diagnosis.md`).

So every case here runs a real binary against a real git repository:

* V1/V2 -- pytest and vitest each execute a sealed suite whose cases the
  builder's *uncommitted* edit turns from two failures into one pass and one
  failure. The counts are checked against the runner's own summary line, not
  against a scripted dict.
* V3 -- the real `cr.review_builder_output` is driven over a real candidate
  commit, and the exact bytes it measured are pushed through the probe's own
  print path. What the probe admits must equal the `redacted_failures` the
  review artifact carries, which is the same public field the builder is
  mailed. The caller is called rather than re-assembled here, so a change to
  how it collects tokens fails this test instead of being mirrored into it.
* V4 -- every file under the builder checkout is hashed before and after, and
  `git status --porcelain` is compared byte for byte. A measurement that edits
  its subject is the second incident in the lane-diagnosis rule.
* V5 -- no sealed path, and no byte of the sealed file's distinctive literal,
  exists anywhere under the checkout afterwards.
* V6 -- the scratch tree is created outside the checkout and is gone.
* V7 -- the shipped entrypoint, run as `uv run sealed_probe.py` from a
  TypeScript-shaped checkout with no Python project of its own. This is the
  only case that proves the PEP 723 dependency list is complete, because it is
  the only one that resolves it.
* V8 -- a working tree already occupying the sealed path refuses with one
  fixed line and no counts, at `probe_tree` and again at `main`.

Every one of them runs for both runners.
"""

from __future__ import annotations

import atexit
import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

from adw_modules import code_review as cr  # noqa: E402
from adw_modules import hidden_vault as hv  # noqa: E402
from adw_modules import plan_compiler  # noqa: E402
from adw_modules import private_review as pr  # noqa: E402
from adw_modules import scheduler_types as st  # noqa: E402
from adw_modules import sealed_probe as sp  # noqa: E402
from adw_modules import tests_chain as tc  # noqa: E402
from adw_modules.lifecycle import ArtifactStore, now_iso  # noqa: E402

INTEGRATION_REF = "refs/heads/main"

#: A quoted string in the sealed source, long enough that
#: `collect_private_tokens` treats it as a secret, short enough that neither
#: runner truncates it inside its own comparison rendering. A truncated literal
#: would sail past an exact-match assertion while still handing the builder a
#: readable prefix, so the prefix is asserted separately below.
SECRET_LITERAL = "SECRET_REFUND_LABEL_VALUE"
SECRET_PREFIX = SECRET_LITERAL[:12]

_VITEST_CACHE: dict[str, object] = {}


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            "git {0} -> {1}: {2}".format(
                " ".join(args), result.returncode, result.stderr
            )
        )
    return result.stdout.strip()


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _sha_map(root: Path) -> dict[str, str]:
    """Every path under `root` and its content hash, `.git` excluded.

    Directories and symlinks are recorded by kind rather than by content, so a
    probe that created or removed either is caught as surely as one that
    rewrote a file.
    """
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        parts = path.relative_to(root).parts
        if ".git" in parts:
            continue
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            out[rel] = "symlink:" + os.readlink(path)
        elif path.is_dir():
            out[rel] = "dir"
        else:
            out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def _porcelain(checkout: Path) -> bytes:
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(checkout),
        capture_output=True,
        check=False,
    )
    return result.stdout


def _vitest_modules() -> Path | None:
    """A directory holding a real `node_modules/.bin/vitest`, installed once.

    Installed for real rather than faked: the point of the vitest case is what
    the binary does with its own argv and what it prints, and neither survives
    a stand-in. `MAESTRO_TEST_VITEST_MODULES` short-circuits the install for a
    machine that already has one.
    """
    if "root" in _VITEST_CACHE:
        return _VITEST_CACHE["root"]  # type: ignore[return-value]
    _VITEST_CACHE["root"] = None
    declared = os.environ.get("MAESTRO_TEST_VITEST_MODULES")
    if declared and (Path(declared) / "node_modules" / ".bin" / "vitest").exists():
        _VITEST_CACHE["root"] = Path(declared).resolve()
        return _VITEST_CACHE["root"]  # type: ignore[return-value]
    npm = shutil.which("npm")
    if npm is None:
        return None
    root = Path(tempfile.mkdtemp(prefix="maestro-probe-vitest-")).resolve()
    atexit.register(shutil.rmtree, str(root), True)
    (root / "package.json").write_text(
        '{"name":"maestro-probe-vitest","private":true,"version":"0.0.0",'
        '"type":"module"}\n',
        encoding="utf-8",
    )
    result = subprocess.run(
        [npm, "install", "--no-audit", "--no-fund", "vitest"],
        cwd=str(root),
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    if result.returncode != 0:
        return None
    if not (root / "node_modules" / ".bin" / "vitest").exists():
        return None
    _VITEST_CACHE["root"] = root
    return root


def _request(*, run_id: str, lane_id: str, input_digest: str) -> pr.VaultLaneRequest:
    return pr.VaultLaneRequest(
        run_id=run_id,
        lane_id=lane_id,
        plan_revision=1,
        spec_digest=_digest("spec"),
        lane_projection_digest=_digest("projection"),
        input_digest=input_digest,
    )


class _RealProbeCase:
    """One fixture repository, one sealed suite, one real runner.

    Subclasses name the runner's files and nothing else. Every case lives
    here rather than on a subclass, so each one runs for both runners: the
    probe is one code path, and a property that holds for pytest and not for
    vitest is a property that has not been proved. vitest is the runner from
    the `vitest list --json <paths>` incident, so it is the last one that may
    be exempted from the sha256 invariance check or from a real `main()`.
    """

    run_id = "run-probe-real"
    tests_lane = "lane-tests"
    build_lane = "lane-build"

    #: Repository path of the sealed test file.
    TEST_PATH: str = ""
    #: The sealed test source, held only in the vault.
    TEST_SOURCE: str = ""
    #: Committed source: both sealed cases fail against it.
    BASE_SOURCE: str = ""
    #: The builder's uncommitted edit: one sealed case passes, one fails.
    EDIT_PATH: str = ""
    EDIT_SOURCE: str = ""
    #: Runner literal, and the substring its summary line carries for
    #: "one failed, one passed". Asserted against the runner's own stdout so
    #: the counts are checked against the measurement, not against this file.
    RUNNER: str = ""
    SUMMARY_MIXED: tuple[str, ...] = ()

    def repo_files(self) -> dict[str, str]:
        raise NotImplementedError

    def skip_without_runner(self) -> None:
        return None

    def runtime_root(self) -> Path:
        return self.repo

    # -- fixture ------------------------------------------------------------

    def setUp(self) -> None:  # noqa: N802 -- unittest's name
        self.skip_without_runner()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "config", "user.email", "harness@example.invalid")
        _git(self.repo, "config", "user.name", "Harness")
        _git(self.repo, "config", "core.hooksPath", str(self.root / "no-hooks"))
        for rel, body in self.repo_files().items():
            target = self.repo.joinpath(*rel.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "base")
        self.head = _git(self.repo, "rev-parse", "HEAD")
        self.state = self.root / "state"
        self.sealed = self._seal()
        self.vault = hv.ensure_vault(self.state, self.run_id)
        self.files = tc.sealed_private_files(self.vault, self.sealed)
        self.checkout = hv.linked_worktree(
            self.repo, self.root / "checkout", self.head
        )
        self.provision()

    def tearDown(self) -> None:  # noqa: N802 -- unittest's name
        self._tmp.cleanup()

    def provision(self) -> None:
        """Whatever the runner needs beside the repository. pytest needs none."""
        return None

    def _seal(self) -> st.LaneArtifact:
        draft = tc.write_test_draft(
            request=_request(
                run_id=self.run_id,
                lane_id=self.tests_lane,
                input_digest=_digest("draft"),
            ),
            state_root=self.state,
            run_repo=self.repo,
            integration_ref=INTEGRATION_REF,
            files={self.TEST_PATH: self.TEST_SOURCE},
            public_contract={
                "acceptance_criteria": ["a refusal is labelled"],
                "declared_outputs": [self.TEST_PATH],
            },
            worktrees_root=self.root / "worktrees",
        )
        tokens = tc.draft_private_tokens(
            state_root=self.state, run_id=self.run_id, draft=draft
        )
        review = tc.review_test_draft(
            request=_request(
                run_id=self.run_id,
                lane_id=self.tests_lane,
                input_digest=_digest("review"),
            ),
            verdict=st.ReviewerVerdict.PASS,
            findings=(),
            test_draft=draft,
            private_tokens=tokens,
        )
        builder = hv.linked_worktree(self.repo, self.root / "seal-builder", self.head)
        return tc.seal_accepted_tests(
            request=_request(
                run_id=self.run_id,
                lane_id=self.tests_lane,
                input_digest=_digest("seal"),
            ),
            state_root=self.state,
            run_repo=self.repo,
            builder_worktree=builder,
            test_draft=draft,
            test_review=review,
        )

    def write_builder_edit(self) -> None:
        target = self.checkout.joinpath(*self.EDIT_PATH.split("/"))
        target.write_text(self.EDIT_SOURCE, encoding="utf-8")

    # -- the probe, with the real runner and a pass-through spy -------------

    @contextlib.contextmanager
    def spy_suite(self):
        """Record what the real `_run_sealed_suite` was handed and returned.

        A pass-through, not a stub: the real function is called, so the runner
        really runs. The recording exists because `ProbeResult` deliberately
        carries no raw output, and V3 has to redact the very bytes the runner
        printed rather than bytes this file invented.
        """
        seen: dict = {"calls": []}
        real = cr._run_sealed_suite

        def wrapper(tree, files, *, gate, runtime_root):
            tree = Path(tree)
            seen["tree"] = tree
            seen["sealed_present"] = (tree / self.TEST_PATH).is_file()
            run, refusal = real(tree, files, gate=gate, runtime_root=runtime_root)
            seen["run"] = run
            seen["refusal"] = refusal
            seen["calls"].append({"tree": tree, "run": run, "refusal": refusal})
            return run, refusal

        with mock.patch.object(cr, "_run_sealed_suite", side_effect=wrapper):
            yield seen

    def probe(self, *, scratch_parent: Path | None = None):
        with self.spy_suite() as seen:
            result = sp.probe_tree(
                self.checkout,
                repo=self.repo,
                vault=self.vault,
                private_files=self.files,
                gate=None,
                provision_argv=(),
                provision_timeout_s=None,
                runtime_root=self.runtime_root(),
                scratch_parent=scratch_parent,
                run_id=self.run_id,
                lane_id=self.tests_lane,
                sealed_ref=self.sealed.artifact_ref,
                sealed_digest=self.sealed.payload["sealed_digest"],
            )
        return result, seen

    def assert_no_secret(self, text: str, label: str) -> None:
        self.assertNotIn(SECRET_LITERAL, text, label)
        self.assertNotIn(
            SECRET_PREFIX,
            text,
            "{0}: a truncated rendering leaked a readable prefix of the "
            "sealed expected literal".format(label),
        )

    # -- V1 / V2 ------------------------------------------------------------

    def test_real_runner_counts_match_what_the_runner_did(self) -> None:
        self.write_builder_edit()

        result, seen = self.probe()

        run = seen["run"]
        self.assertIsNone(
            seen["refusal"],
            "the sealed suite refused rather than measuring: {0}".format(
                str(run.get("output"))[-800:]
            ),
        )
        self.assertTrue(seen["sealed_present"], "the sealed blob never reached the tree")
        self.assertEqual(run["runner"], self.RUNNER)
        output = str(run["output"])
        for fragment in self.SUMMARY_MIXED:
            self.assertIn(
                fragment,
                output,
                "the runner's own summary does not read as one pass and one "
                "failure:\n{0}".format(output[-1500:]),
            )
        self.assertEqual(
            dict(result.counts),
            {
                "executed": 2,
                "passed": 1,
                "failed": 1,
                "errored": 0,
                "skipped": 0,
            },
            "the probe's counts disagree with the runner:\n{0}".format(output[-1500:]),
        )
        self.assertFalse(result.withheld, "the redaction backstop withheld the lines")
        self.assertTrue(
            result.failure_lines,
            "a failing sealed case produced no failure line:\n{0}".format(
                output[-1500:]
            ),
        )
        rendered = sp.render(result)
        self.assert_no_secret(rendered, "the rendered probe output")
        self.assertNotIn(str(self.vault), rendered)
        self.assertNotIn(str(seen["tree"]), rendered)

    def test_the_uncommitted_edit_is_what_was_measured(self) -> None:
        """HEAD alone fails both cases, so a pass proves the overlay ran."""
        clean, _seen = self.probe()
        self.assertEqual(
            dict(clean.counts),
            {
                "executed": 2,
                "passed": 0,
                "failed": 2,
                "errored": 0,
                "skipped": 0,
            },
            "the committed source should fail both sealed cases",
        )

        self.write_builder_edit()
        edited, _seen = self.probe()

        self.assertEqual(edited.counts["passed"], 1)
        self.assertEqual(edited.counts["failed"], 1)

    # -- V3 -----------------------------------------------------------------

    def review_candidate(self):
        """Drive the real `cr.review_builder_output` over a real candidate.

        Not a re-assembly of its argument list: the caller under comparison is
        the caller itself, so a change to how it collects tokens, derives the
        bound surface, or names its results ref fails the parity assertion
        below instead of being mirrored into it by this file.

        It needs an immutable candidate, so the builder's edit is committed
        here and pinned under `refs/maestro/candidates/`, which
        `measure_candidate` requires. The suite runner is never stubbed --
        `measure_candidate` runs it for real against the materialized commit,
        and the pass-through spy only records the bytes it printed.
        """
        _git(self.checkout, "add", self.EDIT_PATH)
        _git(self.checkout, "commit", "-qm", "candidate")
        candidate_sha = _git(self.checkout, "rev-parse", "HEAD")
        candidate_ref = "refs/maestro/candidates/{0}/{1}".format(
            self.build_lane, candidate_sha
        )
        _git(self.repo, "update-ref", candidate_ref, candidate_sha)
        scratch = self.root / "review-scratch"
        scratch.mkdir(parents=True, exist_ok=True)
        request = _request(
            run_id=self.run_id,
            lane_id=self.build_lane,
            input_digest=_digest("candidate-review"),
        )
        with self.spy_suite() as seen:
            artifact = cr.review_builder_output(
                request=request,
                state_root=self.state,
                candidate_repo=self.repo,
                candidate_sha=candidate_sha,
                candidate_ref=candidate_ref,
                builder_base_sha=self.head,
                sealed_bundle=self.sealed,
                # The suite is red, so `review_builder_output` turns this into
                # REVISE itself and supplies its own located finding. Passing
                # PASS keeps this file from inventing a finding the reviewer
                # never made.
                verdict=st.ReviewerVerdict.PASS,
                scratch_root=scratch,
                architecture_constraints=(
                    "add no dependency the plan does not declare",
                ),
                gate=None,
                runtime_root=self.runtime_root(),
            )
        return artifact, seen

    def test_replay_parity_with_the_review_redaction_path(self) -> None:
        """What the probe admits and what the review mails must be one thing.

        Both sides are real here. The probe runs the suite against the working
        tree; `cr.review_builder_output` runs it again against the committed
        candidate and builds its own token set, allow-list and results ref.
        The bytes one of those runs printed are then pushed through the
        probe's own print path, and the lines it admits must equal the
        `redacted_failures` the review artifact carries -- the same public
        field the builder is mailed. A difference is a leak or a regression.
        """
        self.write_builder_edit()

        probe_result, probe_seen = self.probe()
        probe_tree_path = probe_seen["tree"]

        artifact, review_seen = self.review_candidate()

        self.assertEqual(
            len(review_seen["calls"]),
            1,
            "the review measured more than once, so which bytes are being "
            "compared is ambiguous",
        )
        review_run = review_seen["run"]
        self.assertIsNone(review_seen["refusal"])
        review_lines = tuple(artifact.payload["redacted_failures"])
        self.assertTrue(
            review_lines, "the review artifact carried no redacted failure line"
        )

        # The probe's print path, over the exact bytes the review measured and
        # with the same token inputs the review had -- no scratch path, because
        # the review has none to hide. Anything but equality here is the two
        # paths disagreeing about what the builder may see.
        replayed = sp._result_from_run(
            review_run,
            vault=self.vault,
            files=self.files,
            sealed_digest=self.sealed.payload["sealed_digest"],
            sealed_ref=self.sealed.artifact_ref,
            run_id=self.run_id,
            lane_id=self.build_lane,
            secret_paths=(),
        )
        self.assertFalse(replayed.withheld)
        self.assertEqual(
            list(replayed.failure_lines),
            list(review_lines),
            "the probe's print path and review_builder_output admit different "
            "lines for identical bytes",
        )
        self.assertEqual(
            dict(replayed.counts),
            dict(artifact.payload["public_result_summary"]),
            "the probe's counts and the review's public summary disagree",
        )

        # The probe's one addition: its own scratch tree joins the token set,
        # because that path is invented by this process and is nobody's to
        # read. Additive only -- it removes a path and admits the same lines.
        review_tree = review_seen["tree"]
        guarded = sp._result_from_run(
            review_run,
            vault=self.vault,
            files=self.files,
            sealed_digest=self.sealed.payload["sealed_digest"],
            sealed_ref=self.sealed.artifact_ref,
            run_id=self.run_id,
            lane_id=self.build_lane,
            secret_paths=(review_tree.parent, review_tree),
        )
        self.assertEqual(len(guarded.failure_lines), len(review_lines))
        for line in guarded.failure_lines:
            self.assertNotIn(str(review_tree), line)
            self.assertNotIn(str(review_tree.parent), line)

        # And the probe's own live run, on its own real bytes, admits the same
        # number of lines and hands over neither the sealed literal nor the
        # scratch tree it measured in.
        self.assertEqual(
            len(probe_result.failure_lines),
            len(review_lines),
            "the probe's own run admitted a different number of lines than the "
            "review did for the same candidate:\nprobe:  {0!r}\nreview: {1!r}".format(
                probe_result.failure_lines, review_lines
            ),
        )
        for line in probe_result.failure_lines:
            self.assertNotIn(str(probe_tree_path), line)
            self.assertNotIn(str(probe_tree_path.parent), line)
        self.assert_no_secret(
            "\n".join(probe_result.failure_lines), "the probe's own lines"
        )
        self.assert_no_secret("\n".join(review_lines), "the review's own lines")

    # -- V5 -----------------------------------------------------------------

    def test_no_sealed_test_survives_under_the_checkout(self) -> None:
        self.write_builder_edit()

        self.probe()

        sealed_path = self.checkout.joinpath(*self.TEST_PATH.split("/"))
        self.assertFalse(
            sealed_path.exists(),
            "the sealed test path exists in the builder checkout",
        )
        for path in sorted(self.checkout.rglob("*")):
            if ".git" in path.relative_to(self.checkout).parts or not path.is_file():
                continue
            body = path.read_bytes()
            self.assertNotIn(
                SECRET_LITERAL.encode("utf-8"),
                body,
                "{0} in the builder checkout holds the sealed literal".format(
                    path.relative_to(self.checkout).as_posix()
                ),
            )

    # -- V6 -----------------------------------------------------------------

    def test_scratch_tree_is_outside_the_checkout_and_removed(self) -> None:
        parent = self.root / "scratch"
        self.write_builder_edit()

        _result, seen = self.probe(scratch_parent=parent)

        tree = seen["tree"]
        self.assertFalse(
            sp._is_inside(tree, self.checkout),
            "the probe measured inside the checkout it was measuring",
        )
        self.assertTrue(sp._is_inside(tree, parent))
        self.assertFalse(tree.exists(), "the scratch tree outlived the probe")
        self.assertFalse(tree.parent.exists(), "the scratch root outlived the probe")
        self.assertEqual(
            list(parent.iterdir()), [], "the scratch parent was left dirty"
        )

    # -- ledger and registry, for the cases that go through `resolve_lane` --

    def plan_document(self) -> dict:
        return {
            "schema_version": "maestro-plan.artifact-factory.v1",
            "lanes": [
                {
                    "id": self.tests_lane,
                    "lane_kind": "tests",
                    "needs": [],
                    "outputs": [self.TEST_PATH],
                    "spec": {
                        "goal": "seal the refund cases",
                        "integration": {"integration_branch": INTEGRATION_REF},
                        "gate": {
                            "runner": self.RUNNER,
                            "argv": [self.TEST_PATH],
                            "cwd": ".",
                            "min_cases": 1,
                        },
                    },
                    "acceptance": ["a refusal is labelled"],
                },
                {
                    "id": self.build_lane,
                    "lane_kind": "build",
                    "needs": [self.tests_lane],
                    "outputs": [self.EDIT_PATH],
                    "spec": {
                        "goal": "label the refusal",
                        "integration": {"integration_branch": INTEGRATION_REF},
                    },
                    "acceptance": ["a refusal is labelled"],
                },
            ],
        }

    def build_ledger(self) -> Path:
        plan_path = self.root / "plan.json"
        plan_path.write_bytes(
            json.dumps(
                self.plan_document(), sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        )
        compiled = plan_compiler.compile_plan(
            plan_path.read_bytes(), plan_revision=1, plan_artifact_ref=str(plan_path)
        )
        main = _digest("main")[:40]
        binding = st.RunBinding(
            runtime_state_root=str(self.state),
            runtime_state_fingerprint=_digest("state"),
            integration_ref=st.integration_ref(self.run_id),
            integration_initial_sha=main,
            target_repository_root=str(self.repo),
            target_git_common_dir=str(self.repo / ".git"),
            target_worktree_git_dir=str(self.repo / ".git"),
            target_object_format="sha1",
            target_repository_fingerprint=_digest("repo"),
            target_sync_journal_fingerprint=_digest("journal"),
            target_initial_main_sha=main,
            target_main_ref=INTEGRATION_REF,
        )
        database = self.state / "lifecycle.sqlite3"
        store = ArtifactStore(database)
        try:
            store.create_run(self.run_id, compiled, binding)
            lane = next(
                item for item in compiled.lanes if item.lane_id == self.tests_lane
            )
            store.conn.execute(
                "INSERT INTO lane_artifacts(artifact_id, run_id, lane_id, sequence, "
                "completed_stage, artifact_kind, plan_revision, spec_digest, "
                "lane_projection_digest, input_digest, output_digest, artifact_ref, "
                "payload_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    _digest("artifact:current"),
                    self.run_id,
                    self.tests_lane,
                    1,
                    st.LaneStage.TESTS_SEALED.value,
                    st.ArtifactKind.SEALED_TEST_BUNDLE.value,
                    1,
                    lane.spec_digest,
                    lane.lane_projection_digest,
                    self.sealed.input_digest,
                    self.sealed.output_digest,
                    self.sealed.artifact_ref,
                    json.dumps(st.json_ready(self.sealed.payload), sort_keys=True),
                    now_iso(),
                ),
            )
            store.conn.commit()
        finally:
            store.close()
        registry = self.root / "registry.json"
        registry.write_text(
            json.dumps(
                {
                    "installations": [
                        {
                            "database": str(database),
                            "plans_dir": str(self.root),
                            "repository": str(self.repo),
                            "state": str(self.state),
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        return registry

    # -- V4 -----------------------------------------------------------------

    def test_the_probe_leaves_the_checkout_byte_identical(self) -> None:
        self.write_builder_edit()
        registry = self.build_ledger()
        before = _sha_map(self.checkout)
        before_status = _porcelain(self.checkout)
        self.assertIn(self.EDIT_PATH, before)

        self.probe()

        self.assertEqual(
            _sha_map(self.checkout),
            before,
            "probe_tree changed a file under the builder checkout",
        )
        self.assertEqual(_porcelain(self.checkout), before_status)

        stdout = io.StringIO()
        with mock.patch.dict(
            os.environ, {"MAESTRO_REGISTRY": str(registry)}
        ), contextlib.redirect_stdout(stdout):
            code = sp.main(
                [
                    "--run",
                    self.run_id,
                    "--lane",
                    self.build_lane,
                    "--checkout",
                    str(self.checkout),
                ]
            )

        self.assertEqual(code, 0, stdout.getvalue())
        self.assertEqual(
            _sha_map(self.checkout),
            before,
            "main() changed a file under the builder checkout",
        )
        self.assertEqual(_porcelain(self.checkout), before_status)

    # -- V8, at the entrypoint ----------------------------------------------

    def test_main_prints_the_collision_line_and_exits_zero(self) -> None:
        """A collision is a verdict about the candidate, not a broken probe.

        So it leaves through stdout with a zero exit, the way a measurement
        does, rather than through the stderr path that means the probe itself
        could not run. The builder reads one line and nothing else: no counts,
        and not the sealed path that caused it.
        """
        self.write_builder_edit()
        registry = self.build_ledger()
        collide = self.checkout.joinpath(*self.TEST_PATH.split("/"))
        collide.parent.mkdir(parents=True, exist_ok=True)
        collide.write_text("# the builder wrote here\n", encoding="utf-8")
        before = _sha_map(self.checkout)
        before_status = _porcelain(self.checkout)

        stdout = io.StringIO()
        with mock.patch.dict(
            os.environ, {"MAESTRO_REGISTRY": str(registry)}
        ), contextlib.redirect_stdout(stdout):
            code = sp.main(
                [
                    "--run",
                    self.run_id,
                    "--lane",
                    self.build_lane,
                    "--checkout",
                    str(self.checkout),
                ]
            )

        self.assertEqual(code, 0, stdout.getvalue())
        printed = stdout.getvalue()
        self.assertEqual(printed.splitlines(), [sp.COLLISION_LINE])
        for key in sp.COUNT_KEYS:
            self.assertNotIn(key + ":", printed, "a count was printed beside a refusal")
        self.assertNotIn(self.TEST_PATH, printed)
        self.assertNotIn(str(self.state), printed)
        self.assertNotIn(str(self.vault), printed)
        self.assertEqual(_sha_map(self.checkout), before)
        self.assertEqual(_porcelain(self.checkout), before_status)

    # -- V7 -----------------------------------------------------------------

    def test_the_shipped_entrypoint_runs_from_a_typescript_checkout(self) -> None:
        if shutil.which("uv") is None:
            self.skipTest("no uv on PATH; the shipped entrypoint is run with uv")
        self.assertFalse(
            (self.checkout / "pyproject.toml").exists(),
            "the checkout must not be a Python project for this to prove anything",
        )
        self.assertTrue((self.checkout / "package.json").is_file())
        self.write_builder_edit()
        registry = self.build_ledger()

        argv = [
            "uv",
            "run",
            str(ADWS / "sealed_probe.py"),
            "--run",
            self.run_id,
            "--lane",
            self.build_lane,
            "--checkout",
            str(self.checkout),
        ]
        env = dict(os.environ)
        env["MAESTRO_REGISTRY"] = str(registry)
        result = subprocess.run(
            argv,
            cwd=str(self.checkout),
            capture_output=True,
            text=True,
            timeout=900,
            env=env,
            check=False,
        )

        self.assertEqual(
            result.returncode,
            0,
            "the shipped entrypoint refused:\nstdout:\n{0}\nstderr:\n{1}".format(
                result.stdout, result.stderr
            ),
        )
        expected = io.StringIO()
        with mock.patch.dict(
            os.environ, {"MAESTRO_REGISTRY": str(registry)}
        ), contextlib.redirect_stdout(expected):
            code = sp.main(
                [
                    "--run",
                    self.run_id,
                    "--lane",
                    self.build_lane,
                    "--checkout",
                    str(self.checkout),
                ]
            )
        self.assertEqual(code, 0)
        self.assertEqual(
            result.stdout.strip().splitlines(),
            expected.getvalue().strip().splitlines(),
            "the entrypoint printed something other than render(result)",
        )
        self.assertEqual(result.stdout.splitlines()[0], "executed: 2")
        self.assertNotIn(str(self.state), result.stdout)
        self.assertNotIn(str(self.vault), result.stdout)
        self.assert_no_secret(result.stdout, "the entrypoint's stdout")


    # -- V8 -----------------------------------------------------------------

    def test_a_colliding_working_tree_refuses_with_one_line(self) -> None:
        self.write_builder_edit()
        collide = self.checkout.joinpath(*self.TEST_PATH.split("/"))
        collide.parent.mkdir(parents=True, exist_ok=True)
        collide.write_text("# the builder wrote here\n", encoding="utf-8")
        before = _sha_map(self.checkout)
        before_status = _porcelain(self.checkout)

        with self.spy_suite() as seen:
            result = sp.probe_tree(
                self.checkout,
                repo=self.repo,
                vault=self.vault,
                private_files=self.files,
                gate=None,
                provision_argv=(),
                provision_timeout_s=None,
                runtime_root=self.runtime_root(),
                run_id=self.run_id,
                lane_id=self.tests_lane,
                sealed_ref=self.sealed.artifact_ref,
                sealed_digest=self.sealed.payload["sealed_digest"],
            )

        self.assertTrue(result.collision)
        self.assertNotIn("run", seen, "the sealed suite ran despite the collision")
        self.assertEqual(sp.render(result), sp.COLLISION_LINE)
        self.assertEqual(
            sp.render(result),
            "refused: your working tree holds a path the sealed suite owns; "
            "the factory will refuse this candidate",
        )
        self.assertNotIn(self.TEST_PATH, sp.render(result))
        self.assertEqual(_sha_map(self.checkout), before)
        self.assertEqual(_porcelain(self.checkout), before_status)


class RealPytestProbeTest(_RealProbeCase, unittest.TestCase):
    """pytest, for real, from a checkout that is not a Python project.

    `package.json` and no `pyproject.toml` is the FDAdb shape the probe was
    written for, and it is what forces the entrypoint to resolve its own
    dependencies rather than the checkout's. The vitest fixture is that shape
    too, so both classes run the entrypoint case; a Python suite living in a
    TypeScript repository is the harder half and is worth keeping.
    """

    TEST_PATH = "tests/test_refund_secret.py"
    TEST_SOURCE = (
        "from refund import label, refund\n"
        "\n"
        "\n"
        "def test_keeps_a_positive_amount():\n"
        "    assert refund(5) == 5\n"
        "\n"
        "\n"
        "def test_labels_the_refusal():\n"
        '    assert label(-1) == "'
        + SECRET_LITERAL
        + '"\n'
    )
    BASE_SOURCE = (
        "def refund(amount):\n"
        '    raise NotImplementedError("refund is not written yet")\n'
        "\n"
        "\n"
        "def label(amount):\n"
        '    raise NotImplementedError("label is not written yet")\n'
    )
    EDIT_PATH = "refund.py"
    EDIT_SOURCE = (
        "def refund(amount):\n"
        "    return amount\n"
        "\n"
        "\n"
        "def label(amount):\n"
        '    return "wrong-label"\n'
    )
    RUNNER = "pytest"
    SUMMARY_MIXED = ("1 failed, 1 passed",)

    def repo_files(self) -> dict[str, str]:
        return {
            # An empty root conftest is what puts the repository root on
            # `sys.path`, so the sealed file's `from refund import ...`
            # resolves to the candidate's own module rather than to nothing.
            "conftest.py": "",
            "refund.py": self.BASE_SOURCE,
            "package.json": (
                '{"name":"sealed-fixture","private":true,"version":"0.0.0"}\n'
            ),
            ".gitignore": "node_modules/\n",
        }


class RealVitestProbeTest(_RealProbeCase, unittest.TestCase):
    """vitest, for real, resolved out of the runtime root's own node_modules."""

    TEST_PATH = "src/refund.test.ts"
    TEST_SOURCE = (
        'import { it, expect } from "vitest";\n'
        'import { label, refund } from "./refund";\n'
        "\n"
        'it("keeps a positive amount", () => {\n'
        "  expect(refund(5)).toBe(5);\n"
        "});\n"
        "\n"
        'it("labels the refusal", () => {\n'
        '  expect(label(-1)).toBe("'
        + SECRET_LITERAL
        + '");\n'
        "});\n"
    )
    BASE_SOURCE = (
        "export function refund(amount: number): number {\n"
        '  throw new Error("refund is not written yet");\n'
        "}\n"
        "\n"
        "export function label(amount: number): string {\n"
        '  throw new Error("label is not written yet");\n'
        "}\n"
    )
    EDIT_PATH = "src/refund.ts"
    EDIT_SOURCE = (
        "export function refund(amount: number): number {\n"
        "  return amount;\n"
        "}\n"
        "\n"
        "export function label(amount: number): string {\n"
        '  return "wrong-label";\n'
        "}\n"
    )
    RUNNER = "vitest"
    SUMMARY_MIXED = ("1 failed | 1 passed",)

    def skip_without_runner(self) -> None:
        self.modules = _vitest_modules()
        if self.modules is None:
            self.skipTest(
                "no vitest could be installed; set MAESTRO_TEST_VITEST_MODULES "
                "to a directory holding node_modules/.bin/vitest"
            )

    def repo_files(self) -> dict[str, str]:
        return {
            "package.json": (
                '{"name":"sealed-fixture","private":true,"version":"0.0.0",'
                '"type":"module"}\n'
            ),
            "src/refund.ts": self.BASE_SOURCE,
            ".gitignore": "node_modules/\n",
        }

    def provision(self) -> None:
        """Bridge a real vitest into the runtime root, the way a repo has one.

        `run_private_suite` resolves vitest against the runtime root and
        `prepare_collect_tree` symlinks that root's `node_modules` into the
        measured tree, so this is the same wiring a deployment has -- not a
        shortcut around it.
        """
        (self.repo / "node_modules").symlink_to(
            self.modules / "node_modules", target_is_directory=True
        )


if __name__ == "__main__":
    unittest.main()
