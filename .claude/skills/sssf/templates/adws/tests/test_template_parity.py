"""Guard the ADW runtime against silent drift between its template checkouts.

The runtime ships from more than one checkout. The maestro repository holds the
template the factory ships from, and the-library holds the install source that
`skills/sssf` copies into a consuming repository. Nothing synchronises the two,
so a revert or a partial edit in one checkout can delete runtime from the other
without ever producing a conflict. That has already happened once, and the loss
was only noticed long afterwards.

These tests turn that loss into a build failure. They compare the two template
checkouts file by file and report exactly which files differ and in which
direction, so the failure names the repair rather than merely announcing that
the copies disagree.

Two modules own the parts of that, and neither owns the other's part:

* `checkout_layout` answers *where the other checkout is*, from git rather than
  from this file's path.
* `tools/runtime_sync` answers *whether two given trees are level*, and is also
  what moves the bytes when they are not. The comparison is not reimplemented
  here, so the definition of "level" this test fails on is the definition its
  `mirror` verb repairs. Two implementations of that would be the very defect
  class the mirror exists to close — a second, uninstrumented copy path — turned
  on this test.

Scope and limits, stated plainly:

* Only the *template* checkouts are compared. A repository that has installed
  the factory holds a deployed instance which is expected to carry its own
  configuration and may legitimately run ahead of the template, so it is not a
  parity peer and this module skips itself there.
* A peer is located by repository directory name beside the *main* working
  tree of this repository, which `checkout_layout` resolves from git rather
  than from this file's path. Every lane authors its changes in a linked
  worktree, where the enclosing directory is `.claude/worktrees/<lane>` and no
  peer has ever been found beside it; resolving the peer from the filesystem
  alone meant this module skipped through every lane it was supposed to guard.
  Renaming or relocating a checkout still makes the peer undiscoverable, and
  the test then skips, naming the path it looked for and how it chose it, and
  warning so the skip appears in a default run. It does not silently pass: when
  the peer repository is present but its runtime directory is missing, that is
  treated as the deletion this module exists to catch, and it fails.
* The files compared on this side are the ones in the working tree the test
  runs from, so a lane's uncommitted template edits are the bytes checked.
* Bytes agreeing on disk is not bytes agreeing in git. A peer whose runtime is
  uncommitted passes here and is still one `git checkout` from losing it.
"""

from __future__ import annotations

import pathlib
import sys
import unittest

ADWS = pathlib.Path(__file__).resolve().parent.parent
for _path in (str(ADWS / "tests"), str(ADWS / "tools")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import checkout_layout                                       # noqa: E402
import runtime_sync                                          # noqa: E402

_REPAIR = (
    "Reconcile the checkouts before landing anything else: "
    "`python3 tools/runtime_sync.py check <source> <destination>` shows the "
    "drift, and `... mirror <source> <destination> --apply` copies the newer "
    "runtime across with a sha256 assertion per file, refusing any destination "
    "file that looks ahead of the source. Then re-run this test."
)


def _peer_locations(checkout):
    """Return (repo_name, repo_root, adws_root) for every other known checkout."""
    peers = []
    for repo_name, layout in checkout_layout.sorted_template_locations():
        if repo_name == checkout.repo_name:
            continue
        peer_repo_root = checkout_layout.checkout_root(checkout, repo_name)
        peers.append((repo_name, peer_repo_root, peer_repo_root / layout))
    return peers


def _resolve_pair():
    """Return (self_name, self_root, peer_name, peer_root) or raise SkipTest."""
    checkout = checkout_layout.identify_template_checkout(ADWS)
    if checkout is None:
        checkout_layout.skip_visibly(
            "this ADW runtime is a deployed instance, not a template checkout, "
            "so it has no parity peer; the check runs in the maestro and "
            "the-library repositories"
        )
    self_name, self_adws = checkout.repo_name, checkout.adws_root

    reasons = []
    for peer_name, peer_repo_root, peer_adws in _peer_locations(checkout):
        if peer_adws.is_dir():
            return self_name, self_adws, peer_name, peer_adws
        if peer_repo_root.is_dir():
            # The peer repository is checked out but its runtime is gone. That is
            # precisely the silent deletion this module exists to catch, so it
            # must fail rather than skip.
            raise AssertionError(
                "{peer} is checked out at {repo} but its ADW runtime directory "
                "{adws} is missing entirely. {repair} ({provenance})".format(
                    peer=peer_name,
                    repo=peer_repo_root,
                    adws=peer_adws,
                    repair=_REPAIR,
                    provenance=checkout.provenance,
                )
            )
        reasons.append("{name} is not checked out at {path}".format(name=peer_name, path=peer_repo_root))

    checkout_layout.skip_visibly(
        "no peer template checkout is present on this machine ({reasons}); "
        "parity cannot be checked from a single checkout. {provenance}".format(
            reasons="; ".join(reasons), provenance=checkout.provenance
        )
    )


class TemplateParityTests(unittest.TestCase):
    def setUp(self):
        self.self_name, self.self_root, self.peer_name, self.peer_root = _resolve_pair()
        self.report = runtime_sync.compare(
            runtime_sync.describe_copy(self.self_root, self.self_name),
            runtime_sync.describe_copy(self.peer_root, self.peer_name),
        )
        # Both endpoints are template checkouts, so nothing is held back --
        # including `maestro.config.yaml`, which is deployment-owned only when a
        # deployment is one of the two. If that ever stops being true here the
        # exclusion has widened past its reason, and this check is where it
        # would otherwise go unnoticed.
        self.assertEqual(
            (), self.report.excluded,
            "two template checkouts compare every file; nothing is deployment-owned "
            "between them",
        )

    def test_both_template_checkouts_hold_the_same_runtime_files(self):
        """A file in one copy and not the other is a deletion, not an edit."""
        if not self.report.missing_files:
            return
        self.fail(self.report.describe(_REPAIR))

    def test_shared_runtime_files_are_byte_identical(self):
        if not self.report.differing:
            return
        self.fail(self.report.describe(_REPAIR))


if __name__ == "__main__":
    unittest.main()
