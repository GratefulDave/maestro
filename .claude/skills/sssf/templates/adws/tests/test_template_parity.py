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
"""

from __future__ import annotations

import os
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import checkout_layout  # noqa: E402  (needs the path above)

# Paths excluded from the comparison. The exclusions are enumerated rather than
# matched by a loose pattern: anything not named here is compared, so a genuinely
# new runtime file cannot slip out of the check by accident.
#
#   __pycache__, .pytest_cache, .ruff_cache, .mypy_cache  interpreter and tool caches
#   .omc, .omp, .omx                                      agent-harness scratch state
#   .venv                                                 a local interpreter, never runtime
#   adw_data, adw_sssf_config, .maestro, .maestro-state   per-instance run state
#   route-receipts                                        per-machine signed route receipts
IGNORED_DIR_NAMES = frozenset(
    {
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        ".omc",
        ".omp",
        ".omx",
        ".venv",
        "adw_data",
        "adw_sssf_config",
        ".maestro",
        ".maestro-state",
        "route-receipts",
    }
)

IGNORED_FILE_NAMES = frozenset({".DS_Store"})

IGNORED_SUFFIXES = frozenset(
    {".pyc", ".pyo", ".pyd", ".db", ".sqlite", ".sqlite3", ".log"}
)

_REPAIR = (
    "Reconcile the checkouts before landing anything else: copy the newer "
    "runtime across so both template checkouts hold the same bytes, then "
    "re-run this test."
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


def _runtime_files(root):
    """Map every compared file under ``root`` to its absolute path."""
    files = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(name for name in dirnames if name not in IGNORED_DIR_NAMES)
        for filename in sorted(filenames):
            if filename in IGNORED_FILE_NAMES:
                continue
            if pathlib.PurePath(filename).suffix in IGNORED_SUFFIXES:
                continue
            path = pathlib.Path(dirpath) / filename
            files[path.relative_to(root).as_posix()] = path
    return files


def _line_count(path):
    try:
        with open(path, "rb") as handle:
            return sum(1 for _ in handle)
    except OSError:
        return -1


def _resolve_pair():
    """Return (self_name, self_root, peer_name, peer_root) or raise SkipTest."""
    checkout = checkout_layout.identify_template_checkout(
        pathlib.Path(__file__).resolve().parent.parent
    )
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
        self.mine = _runtime_files(self.self_root)
        self.theirs = _runtime_files(self.peer_root)

    def _describe(self):
        return "{a} ({a_path}) vs {b} ({b_path})".format(
            a=self.self_name,
            a_path=self.self_root,
            b=self.peer_name,
            b_path=self.peer_root,
        )

    def test_both_template_checkouts_hold_the_same_runtime_files(self):
        only_mine = sorted(set(self.mine) - set(self.theirs))
        only_theirs = sorted(set(self.theirs) - set(self.mine))
        if not only_mine and not only_theirs:
            return

        report = ["The ADW template checkouts disagree on which files exist: " + self._describe()]
        if only_mine:
            report.append(
                "  present in {a} but absent from {b}:".format(a=self.self_name, b=self.peer_name)
            )
            report.extend("    {rel}".format(rel=rel) for rel in only_mine)
        if only_theirs:
            report.append(
                "  present in {b} but absent from {a}:".format(a=self.self_name, b=self.peer_name)
            )
            report.extend("    {rel}".format(rel=rel) for rel in only_theirs)
        report.append("  " + _REPAIR)
        self.fail("\n".join(report))

    def test_shared_runtime_files_are_byte_identical(self):
        shared = sorted(set(self.mine) & set(self.theirs))
        differing = [
            rel
            for rel in shared
            if self.mine[rel].read_bytes() != self.theirs[rel].read_bytes()
        ]
        if not differing:
            return

        report = ["The ADW template checkouts disagree on file contents: " + self._describe()]
        for rel in differing:
            mine_lines = _line_count(self.mine[rel])
            theirs_lines = _line_count(self.theirs[rel])
            if mine_lines == theirs_lines:
                direction = "same line count ({lines}), differing bytes".format(lines=mine_lines)
            elif mine_lines > theirs_lines:
                direction = "{a} is ahead by {delta} lines ({a_lines} vs {b_lines})".format(
                    a=self.self_name,
                    delta=mine_lines - theirs_lines,
                    a_lines=mine_lines,
                    b_lines=theirs_lines,
                )
            else:
                direction = "{b} is ahead by {delta} lines ({b_lines} vs {a_lines})".format(
                    b=self.peer_name,
                    delta=theirs_lines - mine_lines,
                    b_lines=theirs_lines,
                    a_lines=mine_lines,
                )
            report.append("    {rel}: {direction}".format(rel=rel, direction=direction))
        report.append("  " + _REPAIR)
        self.fail("\n".join(report))


if __name__ == "__main__":
    unittest.main()
