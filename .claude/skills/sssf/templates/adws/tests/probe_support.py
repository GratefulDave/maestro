"""Shared fixtures for the probe tests: a real repo, a real accepted suite, a real runner.

Nothing here stubs a runner. `pytest_runner()` is the interpreter running this
suite with `-m pytest`, so every probe execution a test asserts on is the real
JUnit report of a real pytest process over real files.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
if str(ADWS) not in sys.path:
    sys.path.insert(0, str(ADWS))

from adw_modules import code_review as cr  # noqa: E402
from adw_modules import git_publication as gitpub  # noqa: E402
from adw_modules import probes  # noqa: E402
from adw_modules import review_contract as rc  # noqa: E402
from adw_modules import runner_resolution as rr  # noqa: E402
from adw_modules import scheduler_types as st  # noqa: E402
from adw_modules import tests_chain as tc  # noqa: E402
from adw_modules.git_helper import BoundGit  # noqa: E402

INTEGRATION_REF = "refs/heads/main"
TEST_PATH = "tests/test_refund.py"
PRODUCT = "def refund(amount):\n    return amount\n"
FIXED = "def refund(amount):\n    if amount < 0:\n        return None\n    return amount\n"
TEST_SOURCE = "from refund import refund\n\n\ndef test_positive():\n    assert refund(3) == 3\n"
REQUIREMENT = "negative amounts are refused"
INTERFACE = (
    {
        "kind": "function",
        "module": "refund",
        "name": "refund",
        "signature": "refund(amount) -> int | None",
        "errors": [],
    },
)
CONTRACT = {
    "acceptance_criteria": [REQUIREMENT, "zero amounts are refused"],
    "declared_outputs": ["refund.py"],
    "interface": list(INTERFACE),
}
CONSTRAINTS = ("change only declared outputs",)
FAILING_PROBE = (
    "from refund import refund\n\n\n"
    "def test_zero_is_refused():\n    assert refund(0) is None\n"
)
PASSING_PROBE = (
    "from refund import refund\n\n\n"
    "def test_negative_is_refused():\n    assert refund(-1) is None\n"
)


def digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise AssertionError(
            "git {0} -> {1}: {2}".format(" ".join(args), result.returncode, result.stderr)
        )
    return result.stdout.strip()


def pytest_runner() -> rr.ResolvedRunner:
    return rr.ResolvedRunner(
        runner="pytest", executable=sys.executable, launcher_args=("-m", "pytest")
    )


def request(label: str, run_id: str = "run1", lane_id: str = "lane-a") -> rc.LaneRequest:
    return rc.LaneRequest(
        run_id=run_id,
        lane_id=lane_id,
        plan_revision=1,
        spec_digest=digest("spec"),
        lane_projection_digest=digest("projection"),
        input_digest=digest(label),
    )


def write_tree(root: Path, files: dict[str, str]) -> Path:
    for relative, text in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return root


class RepoCase(unittest.TestCase):
    """A repository carrying an accepted suite, and candidates to review."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "harness@example.invalid")
        git(self.repo, "config", "user.name", "Harness")
        git(self.repo, "config", "core.hooksPath", str(self.root / "no-hooks"))
        (self.repo / "refund.py").write_text(PRODUCT)
        (self.repo / "pytest.ini").write_text("[pytest]\npythonpath = .\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "base")
        self.state = self.root / "state"
        self.run_id = "run1"
        self.lane_id = "lane-a"
        self.git = BoundGit(self.repo)

    def sealed(self) -> tuple[st.LaneArtifact, str]:
        binding = gitpub.bind_target_worktree(self.repo, INTEGRATION_REF)
        head = git(self.repo, "rev-parse", "HEAD")
        draft = tc.write_test_draft(
            request=request("draft"),
            binding=binding,
            integration_head=head,
            files={TEST_PATH: TEST_SOURCE},
            public_contract=CONTRACT,
            declared_outputs=[TEST_PATH],
        )
        review = tc.review_test_draft(
            request=request("test-review"),
            verdict=st.ReviewerVerdict.PASS,
            findings=(),
            test_draft=draft,
        )
        accepted = tc.accept_tests(
            request=request("accept"), test_draft=draft, test_review=review
        )
        merged = str(accepted.payload["candidate_sha"])
        git(self.repo, "update-ref", INTEGRATION_REF, merged)
        git(self.repo, "reset", "-q", "--hard", "HEAD")
        return accepted, merged

    def candidate(self, source: str) -> tuple[str, str]:
        (self.repo / "refund.py").write_text(source)
        if git(self.repo, "status", "--porcelain"):
            git(self.repo, "add", "refund.py")
            git(self.repo, "commit", "-qm", "candidate")
        sha = git(self.repo, "rev-parse", "HEAD")
        ref = st.candidate_ref(self.run_id, self.lane_id, digest("build-" + sha))
        git(self.repo, "update-ref", ref, sha)
        return sha, ref

    def measure(self, label: str, accepted, base: str, source: str = PRODUCT):
        """Measure the accepted suite against a candidate, as the scheduler does."""
        sha, ref = self.candidate(source)
        measurement = cr.measure_candidate(
            request=request(label),
            state_root=self.state,
            candidate_repo=self.repo,
            candidate_sha=sha,
            candidate_ref=ref,
            builder_base_sha=base,
            accepted_suite=accepted,
            scratch_root=self.state / "scratch",
        )
        return sha, ref, measurement

    def review(self, label, accepted, base, sha, ref, measurement, verdict, findings=(), probe=None):
        return cr.review_builder_output(
            request=request(label),
            state_root=self.state,
            candidate_repo=self.repo,
            candidate_sha=sha,
            candidate_ref=ref,
            builder_base_sha=base,
            accepted_suite=accepted,
            verdict=verdict,
            findings=findings,
            scratch_root=self.state / "scratch",
            architecture_constraints=CONSTRAINTS,
            measurement=measurement,
            probe_measurement=probe,
        )

    def admit(
        self,
        tree: Path,
        path: str,
        source: str,
        *,
        label: str = "admit",
        candidate_sha: str = "a" * 40,
        requirement: str = REQUIREMENT,
    ) -> dict:
        """Admit a probe the way the scheduler will: run it, freeze it, receipt it."""
        probes.write_probe_tree(tree, {path: source})
        execution = probes.run_probe(pytest_runner(), tree, path, self.state / "scratch")
        outcome = probes.classify_admission(execution)
        assert outcome == probes.ADMISSION_FAILING, (outcome, execution)
        record = self.record_for(path, source, requirement, execution, label, candidate_sha)
        return record

    def record_for(self, path, source, requirement, execution, label, candidate_sha):
        digest_ = probes.source_digest(source)
        pid = probes.probe_id(path, digest_, requirement)
        receipt = probes.make_receipt(
            mode=probes.MODE_ACCEPT,
            probe_id=pid,
            source_sha256=digest_,
            candidate_sha=candidate_sha,
            input_digest=digest(label),
            requirement_sha256=probes.requirement_digest(requirement),
            failure_sha256=probes.failure_digest(
                probes.frozen_case_ids(execution), cr.failure_lines(execution.output)
            ),
            verdict="PASS",
            acceptor_role="test-reviewer",
            author_vendor="openai",
            acceptor_vendor="xai",
            reason="follows from the cited requirement",
        )
        record = probes.make_record(
            path=path,
            source=source,
            blob_oid=self.git.hash_object(source.encode("utf-8"), write=False),
            requirement=requirement,
            intent="zero must be refused too",
            case_ids=probes.frozen_case_ids(execution),
            admitted_in=digest(label),
            acceptance_receipt_id=receipt["receipt_id"],
        )
        return {"record": record, "receipt": receipt}
