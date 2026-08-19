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

The comparison itself is not implemented here. It lives in
`tools/runtime_sync.py`, which is also what the mirror uses to move bytes, so
the definition of "level" a repair is measured against is the same definition
this test fails on. Two implementations of that would be this module's own
defect class — a second, uninstrumented copy path — turned on itself.

Scope and limits, stated plainly:

* Only the *template* checkouts are compared. A repository that has installed
  the factory holds a deployed instance which is expected to carry its own
  configuration and may legitimately run ahead of the template, so it is not a
  parity peer and this module skips itself there.
* A peer is located by repository directory name next to the current checkout.
  Renaming or relocating a checkout makes the peer undiscoverable, and the test
  then skips with the path it looked for. It does not silently pass: when the
  peer repository is present but its runtime directory is missing, that is
  treated as the deletion this module exists to catch, and it fails.
* Bytes agreeing on disk is not bytes agreeing in git. A peer whose runtime is
  uncommitted passes here and is still one `git checkout` from losing it.
"""

from __future__ import annotations

import pathlib
import sys
import unittest

ADWS = pathlib.Path(__file__).resolve().parents[1]
TOOLS = ADWS / "tools"
for _path in (str(ADWS), str(TOOLS)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import runtime_sync                                          # noqa: E402

# Every known template checkout: the repository directory name, and the path of
# the ADW runtime inside it. Owned by `runtime_sync` so the mirror and this
# check cannot disagree about where a template lives.
TEMPLATE_LOCATIONS = runtime_sync.TEMPLATE_LAYOUTS

_REPAIR = (
    "Reconcile the checkouts before landing anything else: run "
    "`python3 tools/runtime_sync.py check <source> <destination>` to see the "
    "drift, then `... mirror <source> <destination> --apply` to copy the newer "
    "runtime across with a sha256 proof per file, and re-run this test."
)


def _sorted_locations():
    return sorted(TEMPLATE_LOCATIONS, key=lambda item: len(item[1].parts), reverse=True)


def _identify_self():
    """Return (repo_name, repo_root, adws_root) for the checkout holding this file.

    Returns None when this file is not sitting in a known template layout, which
    is the case in a repository that has installed the factory.
    """
    adws_root = ADWS
    for repo_name, layout in _sorted_locations():
        parts = layout.parts
        if adws_root.parts[-len(parts) :] == parts:
            repo_root = adws_root.parents[len(parts) - 1]
            return repo_name, repo_root, adws_root
    return None


def _peer_locations(self_repo_name, self_repo_root):
    """Return (repo_name, repo_root, adws_root) for every other known checkout."""
    siblings = self_repo_root.parent
    peers = []
    for repo_name, layout in _sorted_locations():
        if repo_name == self_repo_name:
            continue
        peer_repo_root = siblings / repo_name
        peers.append((repo_name, peer_repo_root, peer_repo_root / layout))
    return peers


def _resolve_pair():
    """Return (self_name, self_root, peer_name, peer_root) or raise SkipTest."""
    identity = _identify_self()
    if identity is None:
        raise unittest.SkipTest(
            "this ADW runtime is a deployed instance, not a template checkout, "
            "so it has no parity peer; the check runs in the maestro and "
            "the-library repositories"
        )
    self_name, self_repo_root, self_adws = identity

    reasons = []
    for peer_name, peer_repo_root, peer_adws in _peer_locations(self_name, self_repo_root):
        if peer_adws.is_dir():
            return self_name, self_adws, peer_name, peer_adws
        if peer_repo_root.is_dir():
            # The peer repository is checked out but its runtime is gone. That is
            # precisely the silent deletion this module exists to catch, so it
            # must fail rather than skip.
            raise AssertionError(
                "{peer} is checked out at {repo} but its ADW runtime directory "
                "{adws} is missing entirely. {repair}".format(
                    peer=peer_name,
                    repo=peer_repo_root,
                    adws=peer_adws,
                    repair=_REPAIR,
                )
            )
        reasons.append("{name} is not checked out at {path}".format(name=peer_name, path=peer_repo_root))

    raise unittest.SkipTest(
        "no peer template checkout is present on this machine ({reasons}); "
        "parity cannot be checked from a single checkout".format(
            reasons="; ".join(reasons)
        )
    )


class TemplateParityTests(unittest.TestCase):
    def setUp(self):
        self.self_name, self.self_root, self.peer_name, self.peer_root = _resolve_pair()
        self.report = runtime_sync.compare(
            runtime_sync.describe_copy(self.self_root, self.self_name),
            runtime_sync.describe_copy(self.peer_root, self.peer_name),
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
