"""A late worker and a resumed review on one input digest, with divergent sources.

The resumed review finishes while the first is still waiting on its acceptor.
Each pins its own probe commit under its own output-addressed ref; the ledger
takes whichever completes first and refuses the other with the real
`complete_stage`; the loser's ref is an unreferenced root that cannot move the
winner's evidence; and after `git gc --prune=now` the winner's sources are
reconstructed from the object database alone.
"""

import unittest

from adw_modules import probes
from adw_modules import scheduler_types as st
from adw_modules.lifecycle import ArtifactCollision
from tests.probe_lane_support import REQUIREMENT, ProbeLaneCase, probe_path, probe_source
from tests.test_tests_lane_handoff import _git

REVISE = st.ReviewerVerdict.REVISE


def variant(source_suffix):
    return probes.ProbeSubmission(
        probe_path("alpha"), probe_source("alpha") + source_suffix, REQUIREMENT, "a.txt carries alpha"
    )


class InterleavingTest(ProbeLaneCase):
    def refs(self):
        listing = _git(
            self.repo, "for-each-ref", "--format=%(refname)",
            "refs/maestro/probes/{0}/{1}/".format(self.run_id, self.lane_id),
        )
        return sorted(line for line in listing.splitlines() if line)

    def late_worker_and_resume(self, late, resumed, bodies):
        """Run the late worker's review, with the resumed review completing inside it."""
        self.start(bodies, [{"submissions": [late]}, {"submissions": [resumed]}, {}])
        self.scheduler._building(self.lane_id)
        nested = [False]
        original = self.actor.accept_probes

        def accept(ctx, role, items, mode):
            if not nested[0]:
                nested[0] = True
                self.scheduler._reviewing_code(self.lane_id)  # completes first
            return original(ctx, role, items, mode)

        self.actor.accept_probes = accept
        with self.assertRaises(ArtifactCollision):
            self.scheduler._reviewing_code(self.lane_id)

    def test_divergent_sources_the_loser_is_refused_and_the_winner_survives_gc(self):
        late, resumed = variant("# late worker\n"), variant("# resumed review\n")
        self.late_worker_and_resume(late, resumed, ["8"])
        (winner,) = self.reviews()
        self.assertIs(self.stage(), st.LaneStage.BUILDING)
        self.assertEqual(len(self.refs()), 2, "each attempt pinned its own commit")
        winner_ref = probes.probe_ref(self.run_id, self.lane_id, winner["probe_commit"])
        self.assertEqual(winner["probe_ref"], winner_ref)
        self.assertIn(winner_ref, self.refs())
        (loser_ref,) = [ref for ref in self.refs() if ref != winner_ref]
        loser_commit = loser_ref.rsplit("/", 1)[1]
        self.assertNotEqual(loser_commit, winner["probe_commit"])
        _git(self.repo, "gc", "--prune=now")
        git = self.target.git()
        self.assertEqual(
            probes.read_pinned_sources(git, winner["probe_commit"]),
            {probe_path("alpha"): resumed.source.encode()},
        )
        self.assertEqual(
            probes.read_pinned_sources(git, loser_commit),
            {probe_path("alpha"): late.source.encode()},
        )
        self.assertEqual(git.read_ref(winner_ref), winner["probe_commit"])

    def test_the_winning_artifact_alone_feeds_the_next_round(self):
        late, resumed = variant("# late worker\n"), variant("# resumed review\n")
        self.late_worker_and_resume(late, resumed, ["8"])
        records, retired, receipts, commit = self.scheduler._accumulated_probes(self.lane_id)
        self.assertEqual(commit, self.reviews()[0]["probe_commit"])
        self.assertEqual(
            self.scheduler._probe_sources(records, retired, commit),
            {probe_path("alpha"): resumed.source},
        )


if __name__ == "__main__":
    unittest.main()
