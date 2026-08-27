"""The reviewer dispatch decision is the ledger's, not a clock's (§16.3 item 136).

`CandidateReviewState` is a two-phase log: PUBLISHED is written *before* a
reviewer prompt is submitted, DISPATCHED after. A crash between the two used to
leave PUBLISHED meaning either "never offered" or "offered, and the process died
before it could say so", and what stood in for the ledger was a ten-second look
for the prompt's record in the reviewer's own transcript, with the expiry read as
"never offered" and answered by offering the prompt again.

That is the launcher defect one consequence milder. The transcript is written at
TURN granularity, turn length is unbounded (§7.6), and a reviewer turn runs
46-461s (§3.6) -- so the window expired on reviewers that were working, and the
answer was a duplicate review whose report overwrote the one already being
written.

The repair is not a longer window. It is writing the ledger edge *before* the
prompt, so PUBLISHED means "never offered" and DISPATCHED means "do not offer
again", and nothing on the path consults an artifact the actor writes.
"""

from __future__ import annotations

import argparse
import inspect
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ADWS = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent
for _path in (str(ADWS), str(TESTS)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import maestro  # noqa: E402
from adw_modules import code_review as cr  # noqa: E402
from adw_modules import launcher as lch  # noqa: E402
from adw_modules import lifecycle as lc  # noqa: E402
from adw_modules import receipt_crypto as rc  # noqa: E402
from adw_modules import scheduler_types as st  # noqa: E402


def a_node(node_id: str = "build") -> st.PlanNode:
    return st.PlanNode(
        node_id=node_id,
        kind=st.NodeKind.AGENT,
        depth=0,
        outputs=("{0}.py".format(node_id),),
        instruction="Implement {0} as the plan declares.".format(node_id),
        gate_command=("pytest",),
        gate_selector="tests/{0}".format(node_id),
    )


class ReviewerDispatchLedgerTests(unittest.TestCase):
    #: The reviewer pane's durable identity, shared by the registered actor
    #: session and the adopted handle. `mark_dispatched` refuses a binding
    #: mismatch, so these must agree for the resume path to run at all.
    TOKEN = "review-run-1-build-a1"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.repo, self.base_sha, self.output_sha = self._repo_with_two_commits()
        self.review_root = self.root / "state" / "review"
        data_dir = self.root / "sssf-data"
        data_dir.mkdir()
        seed = rc.generate_seed()
        self.args = argparse.Namespace(
            run_id="run-1",
            repo=str(self.repo),
            review_root=str(self.review_root),
            review_receipt_dir=str(self.review_root / "receipts"),
            data_dir=str(data_dir),
            verify_key=[rc.seed_to_public_key(seed).hex()],
            signing_seed=seed.hex(),
            reviewer_route="omp",
            reviewer_model="openai-codex/gpt-5.6-sol",
            reviewer_effort="high",
            reviewer_profile="openai-performance",
            reviewer_vendor="openai",
            execution_vendor="anthropic",
            review_timeout_s=60.0,
            reviewer_turn_timeout_s=30.0,
            reviewer_poll_interval_s=0.1,
            review_reject_grade=cr.DEFAULT_REJECT_GRADE,
        )

    def _repo_with_two_commits(self):
        repo = self.root / "repo"
        repo.mkdir()

        def git(*args: str) -> str:
            return subprocess.run(
                ["git", "-C", str(repo), *args],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()

        git("init", "-q")
        git("config", "user.email", "t@example.invalid")
        git("config", "user.name", "t")
        (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
        git("add", "a.py")
        git("commit", "-qm", "base")
        base_sha = git("rev-parse", "HEAD")
        (repo / "a.py").write_text("x = 2\n", encoding="utf-8")
        git("add", "a.py")
        git("commit", "-qm", "output")
        return repo, base_sha, git("rev-parse", "HEAD")

    def _events(self):
        """Every ledger write and every prompt offer, in the order they happen.

        The ORDER is the whole assertion. A test that only checked both
        happened would pass on the arrangement that carried the defect.
        """
        return []

    def _runner(self, events, handle_holder):
        class FakeRunner:
            workspace_label = "wl"

            def launch(_self, spec):
                events.append(("offer", "launch"))
                handle = lch.LaunchHandle(
                    correlation_token=spec.correlation_token,
                    pane_id="w1:p2",
                    agent_name="reviewer",
                    launched_cwd=spec.worktree,
                    transcript_path=self.root / "session.jsonl",
                    environment=spec.environment,
                )
                handle_holder.append(handle)
                return handle

            def resubmit(_self, handle, prompt_path, **kwargs):
                events.append(("offer", "resubmit"))
                return handle

            def adopt(_self, persisted):
                return lch.LaunchHandle(
                    correlation_token=persisted.correlation_token,
                    pane_id=persisted.pane_id,
                    agent_name=persisted.agent_name,
                    launched_cwd=persisted.launched_cwd,
                    transcript_path=persisted.transcript_path,
                )

            def cancel(_self, handle, deadline):
                pass

            def agent_status(_self, handle):
                return "working"

        return FakeRunner()

    def _store_with_published_review(self, events):
        """A ledger holding exactly one PUBLISHED review for the output sha."""
        store = lc.LifecycleStore(self.root / "lifecycle.db")
        self.addCleanup(store.close)
        store.create_run(self.args.run_id, "d" * 64, (a_node(),))
        # The review node is a runtime projection, never authored (§7.3).
        store.ensure_derived_review_node(self.args.run_id, "build", depth=1)
        store.start_attempt(self.args.run_id, "build", self.base_sha)
        store.publish_candidate(
            self.args.run_id, "build", self.output_sha, builder_generation=1
        )
        store.begin_review(
            self.args.run_id,
            "build::review",
            self.output_sha,
            reviewer_generation=1,
        )
        # An ACTIVE reviewer session is what makes this a RESUME rather than a
        # first dispatch: the pane outlived the scheduler, so the question
        # "was this one already given the prompt?" is live. That question is
        # the one the ten-second transcript window used to answer by guessing.
        store.register_actor_session(
            self.args.run_id,
            "build",
            "reviewer",
            generation=1,
            pane_id="w1:p2",
            session_path=str(self.root / "session.jsonl"),
            correlation_token=self.TOKEN,
            tab_id="w1:t1",
        )
        original = store.mark_review_dispatched

        def traced(*args, **kwargs):
            events.append(("ledger", "DISPATCHED"))
            return original(*args, **kwargs)

        store.mark_review_dispatched = traced  # type: ignore[assignment]
        return store

    def _drive(self, store, events):
        """Run one `review(...)` on a resumed dispatch, capturing the order."""
        handle_holder = []
        runner = self._runner(events, handle_holder)

        class CapturedWindow:
            def __init__(self, **kwargs):
                self.launch = kwargs["launch"]
                self.poll_report = kwargs["poll_report"]

        def stub_review_attempt(*, window_factory, **_kwargs):
            window = window_factory(None)
            window.launch()
            return "reviewed"

        with (
            mock.patch.object(
                maestro.agent_pi,
                "catalog",
                lambda: (("openai-codex", "gpt-5.6-sol", 400_000),),
            ),
            mock.patch.object(
                maestro.agent_pi, "context_window", return_value=400_000
            ),
            mock.patch.object(
                maestro.finalization_window, "FinalizationWindow", CapturedWindow
            ),
            mock.patch.object(maestro.code_review, "review_attempt", stub_review_attempt),
        ):
            review = maestro._code_review_runner(self.args, runner, store)
            review(
                None,
                a_node(),
                None,
                self.base_sha,
                self.output_sha,
                resume_existing_dispatch=True,
            )
        return handle_holder

    def test_the_ledger_edge_is_written_before_the_prompt_is_offered(self):
        """Ledger first, prompt second — and the order is the fix.

        Written the other way round, a crash in between leaves PUBLISHED on a
        reviewer that already has the prompt, which is the ambiguity the
        ten-second transcript window existed to guess at.
        """
        events = []
        store = self._store_with_published_review(events)

        self._drive(store, events)

        self.assertIn(("ledger", "DISPATCHED"), events)
        offers = [index for index, item in enumerate(events) if item[0] == "offer"]
        ledgers = [index for index, item in enumerate(events) if item[0] == "ledger"]
        self.assertTrue(offers, "the reviewer was never given the prompt")
        self.assertTrue(ledgers)
        self.assertLess(
            ledgers[0],
            offers[0],
            "the prompt was offered before the ledger recorded it: a crash in "
            "that gap is exactly the ambiguity this ordering removes",
        )

    def test_a_recorded_dispatch_is_never_offered_a_second_time(self):
        """DISPATCHED means do not offer again, with nothing else consulted.

        This is the duplicate review the ten-second window used to buy: the
        reviewer was working, its transcript had not flushed the turn-start
        record yet, and the expiry was read as "never offered".
        """
        events = []
        store = self._store_with_published_review(events)
        store.mark_review_dispatched(
            self.args.run_id,
            "build::review",
            self.output_sha,
            reviewer_generation=1,
        )
        events.clear()

        self._drive(store, events)

        self.assertEqual(
            [item for item in events if item[0] == "offer"],
            [],
            "a dispatch the ledger already records was offered again",
        )

    def test_no_bounded_wait_on_the_submission_record_survives(self):
        """The window itself is gone, not merely unused.

        `wait_for_prompt_submission_record` was a ten-second look at whether a
        turn had recorded itself yet. Turn length is unbounded, so the function
        could not be correct at any timeout, and leaving it in the module is an
        invitation to call it again.
        """
        self.assertFalse(hasattr(lch, "wait_for_prompt_submission_record"))
        source = inspect.getsource(maestro._code_review_runner)
        self.assertNotIn("wait_for_prompt_submission_record", source)
        self.assertNotIn(
            "prompt_submission_recorded",
            source,
            "the reviewer dispatch decision must rest on the ledger, not on an "
            "artifact the actor writes at its own pace",
        )


if __name__ == "__main__":
    unittest.main()
