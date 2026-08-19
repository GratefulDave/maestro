"""Locate this runtime's other checkouts from inside any working tree.

Three modules in this suite have to reach outside their own checkout to do
their job: `test_template_parity` compares the two template copies file by
file, `test_schema_vocabulary_parity` pins this runtime's closed vocabularies
to the Plan IR schema the-library publishes, and `test_plan_admission` reads
that same authoring schema. All three need the same thing first -- the
directory that holds the sibling checkouts -- and each used to derive it by
walking up from ``__file__`` and taking the parent of the repository root.

That derivation is wrong in a linked git worktree, and a linked git worktree is
where the work happens. Every lane authors its changes in one, so the ancestor
that arithmetic lands on is ``.claude/worktrees/<lane>`` rather than the
repository, and the peer is looked for in ``.claude/worktrees/the-library``,
which no machine has. All three modules then skipped, and a skip is silent: the
one mechanical check holding the template copies together had never once looked
at lane work, and reported nothing while not looking.

The repository is asked for its own layout instead. ``git rev-parse
--git-common-dir`` names the main working tree's ``.git`` directory from any
worktree of the repository, including the main one, so its parent is the
repository whose siblings are the peer checkouts. When git cannot answer -- no
git on the path, an exported tree with no repository, a bare repository whose
common directory is not a ``.git`` -- the old filesystem derivation is still
used, because it is right in the ordinary case and this module must not turn a
working check into an error. The reason is carried out in ``provenance`` either
way, so a caller that ends up skipping can say what it looked for and why.

Nothing here decides whether to skip. Each caller owns that: what it needs from
a peer, and which absences are legitimate, differ between the three.
"""

from __future__ import annotations

import collections
import pathlib
import subprocess
import sys
import unittest
import warnings

_TOOLS = str(pathlib.Path(__file__).resolve().parent.parent / "tools")
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)

import runtime_sync  # noqa: E402  (needs the path above)

#: Every known template checkout: the repository directory name, and the path
#: of the ADW runtime inside it. Consumers sort this longest-path-first so that
#: the-library's layout, which is a suffix of maestro's, cannot claim a maestro
#: checkout.
#:
#: The table is *owned* by `tools/runtime_sync`, not by this module, and the
#: direction of that dependency is deliberate. `runtime_sync` is production: it
#: ships with the runtime to every deployment, it needs the same table to tell a
#: template checkout from a deployed instance, and it must not import anything
#: out of `tests/`. This module is test support -- it raises `SkipTest` and
#: warns -- so it is the one that imports. Duplicating the literal instead would
#: leave two answers to "where does a template live", which is the same defect
#: class the mirror in `runtime_sync` exists to close.
TEMPLATE_LOCATIONS = runtime_sync.TEMPLATE_LAYOUTS

#: Asking git for the repository layout is a local metadata read. A bound is
#: still set: a wedged git must degrade this to the filesystem derivation
#: rather than hang a suite that has nothing else to wait for.
GIT_TIMEOUT_S = 30

#: What a checkout of the template runtime looks like from inside it.
#:
#: ``repo_name``     the repository directory name from TEMPLATE_LOCATIONS
#: ``root``          the root of *this* working tree, which is the linked
#:                   worktree when running in one -- the files under test
#: ``adws_root``     the ADW runtime inside ``root``
#: ``neighbourhood`` the directory the peer checkouts sit in
#: ``provenance``    how ``neighbourhood`` was arrived at, for skip messages
Checkout = collections.namedtuple(
    "Checkout", "repo_name root adws_root neighbourhood provenance"
)


class PeerCheckoutMissing(UserWarning):
    """A cross-checkout test declined to run because it found no peer.

    A skip is reported as a single character and nothing else, so the previous
    silence is what let a wrongly-resolved peer path go unnoticed for the whole
    life of a branch. Raising the same sentence as a warning puts it in the run
    summary whether or not anyone passed `-rs`, which is the difference between
    a check that is known not to be running and one that is merely absent.
    """


def skip_visibly(reason):
    """Skip the calling test, and say so where a default run will show it."""
    warnings.warn(reason, PeerCheckoutMissing, stacklevel=3)
    raise unittest.SkipTest(reason)


def sorted_template_locations():
    """TEMPLATE_LOCATIONS, longest layout first."""
    return sorted(TEMPLATE_LOCATIONS, key=lambda item: len(item[1].parts), reverse=True)


def _git_main_working_tree(start):
    """Return (root, detail) for the main working tree of ``start``'s repository.

    ``root`` is None when git cannot answer, and ``detail`` then says why in
    terms a skip message can quote.
    """
    command = ("git", "-C", str(start), "rev-parse", "--git-common-dir")
    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=GIT_TIMEOUT_S,
        )
    except OSError as error:
        return None, "git could not be run ({error})".format(error=error)
    except subprocess.SubprocessError as error:
        return None, "git did not answer ({error})".format(error=error)

    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", "replace").strip()
        return None, (
            "`git rev-parse --git-common-dir` exited {code} in {start}{detail}".format(
                code=completed.returncode,
                start=start,
                detail=": " + stderr if stderr else "",
            )
        )

    raw = completed.stdout.decode("utf-8", "replace").strip()
    if not raw:
        return None, "`git rev-parse --git-common-dir` printed nothing in {start}".format(
            start=start
        )

    common = pathlib.Path(raw)
    if not common.is_absolute():
        # Answered relative to the -C directory, which is where git was pointed.
        common = pathlib.Path(start) / common
    common = common.resolve()

    if common.name != ".git":
        # A bare repository, or a GIT_DIR pointed somewhere unusual. Either way
        # there is no working tree to take the parent of.
        return None, (
            "the git common directory {common} is not a working tree's .git "
            "directory".format(common=common)
        )

    root = common.parent
    if not root.is_dir():
        return None, "the git common directory {common} has no working tree".format(
            common=common
        )
    return root, "the git common directory {common}".format(common=common)


def identify_template_checkout(adws_root):
    """Describe the template checkout holding ``adws_root``, or return None.

    None means this runtime is not sitting in a known template layout, which is
    the case in a repository that has installed the factory: a deployed
    instance carries its own configuration, may legitimately run ahead of the
    template, and has no peer to be compared against.
    """
    adws_root = pathlib.Path(adws_root).resolve()
    for repo_name, layout in sorted_template_locations():
        parts = layout.parts
        if adws_root.parts[-len(parts) :] != parts:
            continue
        root = adws_root.parents[len(parts) - 1]

        main_root, detail = _git_main_working_tree(adws_root)
        if main_root is None:
            provenance = (
                "peers were looked for beside {root}, derived from this file's "
                "path because the repository could not be asked: {detail}".format(
                    root=root, detail=detail
                )
            )
            neighbourhood = root.parent
        else:
            neighbourhood = main_root.parent
            if main_root == root:
                provenance = (
                    "peers were looked for beside the main working tree {main}, "
                    "resolved from {detail}".format(main=main_root, detail=detail)
                )
            else:
                provenance = (
                    "this is the linked worktree {root} of the main working tree "
                    "{main}, so peers were looked for beside {main} rather than "
                    "beside the worktree; resolved from {detail}".format(
                        root=root, main=main_root, detail=detail
                    )
                )
        return Checkout(
            repo_name=repo_name,
            root=root,
            adws_root=adws_root,
            neighbourhood=neighbourhood,
            provenance=provenance,
        )
    return None


def checkout_root(checkout, repo_name):
    """Where ``repo_name``'s checkout is, seen from ``checkout``.

    The checkout under test answers for itself, so a worktree is compared and
    read as the tree it is rather than as the repository it was branched from.
    Any other repository is a sibling of the main working tree.
    """
    if repo_name == checkout.repo_name:
        return checkout.root
    return checkout.neighbourhood / repo_name
