"""The suite a review runs is the suite the factory accepted, byte for byte.

Binds a review to its accepted test suite by content, so "the candidate passed"
is a statement about the cases the test reviewer accepted and not about whatever
happened to be under those paths when the runner started.

Why this is not `tests_chain._manifest_digest`
----------------------------------------------
That digest includes the draft commit, so it identifies a bundle in the vault
and cannot be recomputed from a checkout. This one is a function of the
path-to-blob pairs alone, which means the same value can be derived from the
vault's manifest, from an overlay, or from files sitting in a working tree.
That is the whole point: today the accepted suite is copied into the review
tree from the vault immediately before the runner is invoked, so `verify_suite`
reads back what that copy just wrote. Written this way it says the same thing
when the overlay is gone and the suite is carried in the tree itself, where the
bytes under a test path are whatever that tree's history put there.

What a refusal may say
----------------------
The path and the two blob ids, and nothing else. A test suite's contents are
private -- selectors, expected literals, fixture data -- and a refusal is read
by an operator and written to a log, neither of which is inside the private
boundary. `TestSuiteTampered` is a `SealedEnvironmentError` because that is what
it is: no verdict about the candidate exists, so none may be recorded against
the builder. The operator surface already recognises that class by name.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Mapping

from . import private_review as pr

#: Versioned because the digest reaches artifacts. A later scheme is a new
#: prefix, never a silently different hash under the same one.
SUITE_DIGEST_SCHEMA = "test-suite.v1"

#: Git blob ids are hex of the repository's object hash. Both formats are
#: admitted and the expected id's own length says which, so a sha256 vault is
#: verified with sha256 rather than refused or, worse, compared against a sha1.
_ALGORITHM_BY_LENGTH = {40: "sha1", 64: "sha256"}

#: Stand-ins for "there is no blob id to report", never a hash.
ABSENT = "ABSENT"
SYMLINK = "SYMLINK"
UNKNOWN_OBJECT_FORMAT = "UNKNOWN_OBJECT_FORMAT"


class TestSuiteTampered(pr.SealedEnvironmentError):
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

    `hidden_vault.hash_blob` shells out to `git hash-object -w`, which needs a
    vault and writes the object. Verification neither has a repository to hand
    nor may write anything into the tree it is measuring.
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
    mismatch whatever it points at: the accepted suite is a regular file, and a
    link is a way to make the runner read bytes this check never hashed.
    """
    root = Path(tree)
    for path in sorted(expected):
        accepted = expected[path]
        algorithm = _ALGORITHM_BY_LENGTH.get(len(accepted))
        if algorithm is None:
            raise TestSuiteTampered(path, accepted, UNKNOWN_OBJECT_FORMAT)
        target = root.joinpath(*path.split("/"))
        if target.is_symlink():
            raise TestSuiteTampered(path, accepted, SYMLINK)
        try:
            data = target.read_bytes()
        except OSError:
            raise TestSuiteTampered(path, accepted, ABSENT) from None
        found = blob_id(data, algorithm=algorithm)
        if found != accepted:
            raise TestSuiteTampered(path, accepted, found)
