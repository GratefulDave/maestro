"""The suite a review runs is the suite the factory accepted, byte for byte.

Binds a review to its accepted test suite by content, so "the candidate passed"
is a statement about the cases the test reviewer accepted and not about whatever
happened to be under those paths when the runner started.

Why the digest is a function of paths and blobs alone
------------------------------------------------------
The accepted suite is carried in the tree the runner executes in -- a tests
lane merges it into the integration ref, and every candidate descends from
there -- so the bytes under a test path are whatever that tree's history put
there. A digest over the path-to-blob pairs can be recomputed from any
checkout, which is what lets `verify_suite` say, before the runner is invoked,
that the suite about to run is the one the test reviewer accepted and not a
candidate's rewrite of it. This is the load-bearing half of the immutability
boundary; the other half is `CANDIDATE_TEST_PATH_REFUSED` at admission.

What a refusal may say
----------------------
The path and the two blob ids. `TestSuiteTampered` is a
`SuiteEnvironmentError` because that is what it is: no verdict about the
candidate exists, so none may be recorded against the builder. The operator
surface already recognises that class by name.
"""

from __future__ import annotations

import hashlib
import stat
from pathlib import Path
from typing import Mapping

from . import review_contract as rc

#: Versioned because the digest reaches artifacts. A later scheme is a new
#: prefix, never a silently different hash under the same one.
SUITE_DIGEST_SCHEMA = "test-suite.v1"

#: Git blob ids are hex of the repository's object hash. Both formats are
#: admitted and the expected id's own length says which, so a sha256 repository
#: is verified with sha256 rather than refused or, worse, compared against a sha1.
_ALGORITHM_BY_LENGTH = {40: "sha1", 64: "sha256"}

#: Stand-ins for "there is no blob id to report", never a hash.
ABSENT = "ABSENT"
SYMLINK = "SYMLINK"
UNKNOWN_OBJECT_FORMAT = "UNKNOWN_OBJECT_FORMAT"


class TestSuiteTampered(rc.SuiteEnvironmentError):
    """The tree does not hold the accepted suite. Never a candidate defect.

    A candidate that fails the suite is a defect; a suite that is not the
    accepted suite is not a measurement of the candidate at all, so nothing
    about it may be billed to the builder.
    """

    #: pytest collects classes named `Test*` from any module a test imports.
    __test__ = False

    code = "TEST_SUITE_TAMPERED"

    def __init__(self, path: str, expected: str, actual: str) -> None:
        self.path = path
        self.expected = expected
        self.actual = actual
        super().__init__(
            "{0}:{1}:{2}:{3}".format(self.code, path, expected, actual)
        )


def blob_id(data: bytes, *, algorithm: str = "sha1") -> str:
    """The git object id of `data` as a blob, without a repository.

    `git hash-object -w` needs a repository and writes the object. Verification
    neither has a repository to hand nor may write anything into the tree it
    is measuring.
    """
    header = "blob {0}\0".format(len(data)).encode("ascii")
    return hashlib.new(algorithm, header + data).hexdigest()


def suite_digest(files: Mapping[str, str]) -> str:
    """One value naming exactly this set of paths at exactly these blobs.

    Order-independent by construction, so a manifest read in a different order
    is the same suite. A renamed path is a different suite, because a case that
    moved is a case the runner may no longer collect.
    """
    body = "\n".join(
        "{0}\0{1}".format(path, files[path]) for path in sorted(files)
    )
    return "{0}:{1}".format(
        SUITE_DIGEST_SCHEMA, hashlib.sha256(body.encode("utf-8")).hexdigest()
    )


def verify_suite(expected: Mapping[str, str], tree: Path) -> None:
    """Refuse unless every expected path in `tree` holds its accepted blob.

    Raises on the first mismatch in sorted path order, so the refusal is stable
    across runs rather than dependent on mapping iteration. A symlink is a
    mismatch whatever it points at, at the leaf or at any directory on the way
    to it: the accepted suite is a regular file reached through real
    directories of this tree, and a link anywhere on that route is a way to
    make the runner read bytes this tree does not own. The leaf must be a
    regular file, and the resolved path must stay inside the tree.
    """
    root = Path(tree)
    resolved_root = root.resolve()
    for path in sorted(expected):
        accepted = expected[path]
        algorithm = _ALGORITHM_BY_LENGTH.get(len(accepted))
        if algorithm is None:
            raise TestSuiteTampered(path, accepted, UNKNOWN_OBJECT_FORMAT)
        target = root.joinpath(*path.split("/"))
        _verify_route(root, path, accepted)
        try:
            if not target.resolve().is_relative_to(resolved_root):
                raise TestSuiteTampered(path, accepted, SYMLINK)
            data = target.read_bytes()
        except OSError:
            raise TestSuiteTampered(path, accepted, ABSENT) from None
        found = blob_id(data, algorithm=algorithm)
        if found != accepted:
            raise TestSuiteTampered(path, accepted, found)


def _verify_route(root: Path, path: str, accepted: str) -> None:
    """`lstat` every component from `root` to the leaf; no link, leaf a file.

    A missing component is ABSENT, a link anywhere is SYMLINK, and a leaf that
    exists but is not a regular file (a directory, a fifo) is ABSENT: there is
    no file there to hash.
    """
    parts = path.split("/")
    current = root
    for index, part in enumerate(parts):
        current = current / part
        try:
            mode = current.lstat().st_mode
        except OSError:
            raise TestSuiteTampered(path, accepted, ABSENT) from None
        if stat.S_ISLNK(mode):
            raise TestSuiteTampered(path, accepted, SYMLINK)
        is_leaf = index == len(parts) - 1
        if is_leaf and not stat.S_ISREG(mode):
            raise TestSuiteTampered(path, accepted, ABSENT)
        if not is_leaf and not stat.S_ISDIR(mode):
            raise TestSuiteTampered(path, accepted, ABSENT)
