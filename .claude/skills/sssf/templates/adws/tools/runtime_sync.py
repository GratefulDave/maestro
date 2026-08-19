#!/usr/bin/env python3
"""Move ADW runtime bytes between checkouts with proof, or refuse to move them.

The ADW runtime exists in more than one checkout — the template this factory
ships from, the-library's install source, and every repository the factory has
been installed into — and until this module existed **nothing copied one to
another**. Every reconciliation was a hand-run `cp`, a `git checkout` of a path
from somewhere else, or an operator reading `diff -rq` output. Three things
followed from that, all of them observed rather than imagined:

* A revert in a consuming repository deleted 6,009 lines of runtime from a copy
  and produced no conflict, no diff anyone read, and no error.
* The template fell ~750 lines behind in `maestro.py` alone, missing every plan
  verb in daily use, and the gap was found only when something broke.
* A deployment ran every code review against a placeholder goal for days,
  because the fix for it had landed in the template and never reached the
  runtime that ran.

The shape shared by all three is one sentence: **runtime bytes move between
checkouts by unverified, unrecorded copies, so a copy that overwrites newer work
or drops a file entirely raises nothing and leaves no evidence.** This module is
the answer to that sentence, and every design decision below is one clause of it.

## What it guarantees

1. **Copies are proved, not assumed.** `copy_verified` writes with
   `shutil.copy2`, then reads *both* files back and asserts their sha256 are
   equal. It never uses `git apply`, which on the machine this was written for
   reports success and changes nothing.
2. **Drift is a value, not output.** `compare` returns a `DriftReport` a caller
   can assert on. A file missing from one side is `absent_from_destination` /
   `absent_from_source` — a *different field* from `differing`, because the
   6,009-line loss was an absence and an absence read as "some files differ"
   is a loss that looks like an edit.
3. **Deployment-owned configuration never crosses.** `maestro.config.yaml`
   names a particular installation's lane vendors, models, and concurrency.
   It is excluded from any comparison or mirror where either endpoint is a
   deployment. Between two *template* checkouts it is compared and copied
   normally, because there both copies hold template-shaped values.
4. **Dry-run is the default.** `mirror` plans; `mirror(apply=True)` writes.
   Destroying a copy is never the shorter command.
5. **A destination that looks ahead is refused by name.** Two independent
   signals, either alone sufficient: `DESTINATION_NEWER` (differing bytes and a
   strictly newer mtime) and `DESTINATION_LONGER` (strictly more lines than the
   source). Neither is proof of anything; between them they cover both shapes
   the loss has taken, and the second exists because the first was measured
   against two real deployments and fired on nothing — a freshly checked-out
   source tree is newer than every file in a long-lived destination. Overriding
   is the explicitly named `overwrite_ahead=True` / `--overwrite-ahead`, and the
   refusals it overrode stay in the result rather than disappearing.
6. **Nothing is ever deleted.** Files present only in the destination are
   reported as `left_in_destination` and left where they are. A mirror that
   prunes is the loss mode this module exists to prevent, so it does not prune.

## What it does not do

It has no opinion about *which* copy should win. `compare` reports the direction
of every difference and stops; deciding that the template is authoritative, or
that a deployment's hotfix should flow back, is a human judgement and this module
refuses to encode one. It also says nothing about whether a copy is committed:
bytes agreeing on disk is not bytes agreeing in git, and a checkout whose runtime
is untracked can be rewritten without leaving history no matter what this reports.

Run:  uv run adw_test.py -k runtime_sync
"""

from __future__ import annotations

import argparse
import hashlib
import os
import pathlib
import shutil
import sys
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

TEMPLATE = "template"
DEPLOYMENT = "deployment"

#: Every known template checkout, as (repository directory name, runtime path
#: inside it). This is the single owner of that table.
#:
#: `tests/checkout_layout.py` re-exports it as `TEMPLATE_LOCATIONS` and adds what
#: only a test needs — resolving the peer checkout from git so a linked worktree
#: finds its sibling — but the table itself lives here, because this module is
#: production, ships to every deployment, and must not import out of `tests/`.
#: Two copies of it would be this module's own defect class turned on itself.
TEMPLATE_LAYOUTS: Tuple[Tuple[str, pathlib.PurePosixPath], ...] = (
    ("maestro", pathlib.PurePosixPath(".claude/skills/sssf/templates/adws")),
    ("the-library", pathlib.PurePosixPath("skills/sssf/templates/adws")),
)

#: Directories that are never runtime. Enumerated rather than pattern-matched so
#: that a genuinely new runtime directory cannot fall out of the comparison by
#: accident; a tool nobody has heard of yet has to be added here deliberately.
#:
#:   __pycache__, .pytest_cache, .ruff_cache, .mypy_cache  interpreter/tool caches
#:   .omc, .omp, .omx                                      agent-harness scratch state
#:   .venv                                                 a local interpreter
#:   adw_data, adw_sssf_config, .maestro, .maestro-state   per-instance run state
#:   route-receipts                                        per-machine signed receipts
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

#: Files a deployed instance owns outright. Their content names one particular
#: installation — its lane vendors, its models, its concurrency — so copying one
#: in either direction across a deployment boundary either destroys that
#: installation's configuration or leaks it into the shipped template.
DEPLOYMENT_OWNED = frozenset({"maestro.config.yaml"})

#: Refusal signal: the destination file differs and its mtime is newer than the
#: source's. `copy2` preserves mtime, so a file that was mirrored and then left
#: alone is never newer; one somebody edited afterwards always is.
DESTINATION_NEWER = "DESTINATION_NEWER"

#: Refusal signal: the destination file has strictly more lines than the source.
#:
#: This exists because the mtime signal has a hole big enough to drive the
#: original defect through, and the hole was found by running this module rather
#: than by reasoning about it. A git worktree checkout stamps every file with the
#: checkout time, so a freshly created source tree is newer than *every* file in
#: a long-lived destination and `DESTINATION_NEWER` fires on nothing. Measured
#: against two real deployments, mtime refused 0 files while six held content the
#: template did not have — including one 91 lines longer than the source about to
#: replace it. Line count is a weaker signal than mtime in general and a much
#: stronger one here, because the loss this module exists to stop is bulk
#: deletion, and bulk deletion is exactly what "the destination is longer" looks
#: like from the source's side.
DESTINATION_LONGER = "DESTINATION_LONGER"


class VerificationError(RuntimeError):
    """A copy completed and the destination's bytes are not the source's."""


@dataclass(frozen=True)
class RuntimeCopy:
    """One checkout's ADW runtime directory, and what kind of copy it is."""

    name: str
    root: pathlib.Path
    kind: str

    @property
    def is_template(self) -> bool:
        return self.kind == TEMPLATE

    def __str__(self) -> str:  # pragma: no cover - trivial
        return "{name} ({kind}) at {root}".format(
            name=self.name, kind=self.kind, root=self.root
        )


def classify(root: os.PathLike | str) -> str:
    """TEMPLATE if ``root`` sits at a known template layout, else DEPLOYMENT.

    Classification is by path shape rather than by content, because content is
    exactly what is in question: a deployment that has fallen far behind still
    looks like a template from the inside.
    """
    resolved = pathlib.Path(root).resolve()
    for _name, layout in TEMPLATE_LAYOUTS:
        parts = layout.parts
        if resolved.parts[-len(parts):] == parts:
            return TEMPLATE
    return DEPLOYMENT


def describe_copy(root: os.PathLike | str, name: Optional[str] = None) -> RuntimeCopy:
    """Build a :class:`RuntimeCopy` for ``root``, classifying it by path shape."""
    resolved = pathlib.Path(root).resolve()
    kind = classify(resolved)
    if name is None:
        if kind == TEMPLATE:
            for candidate, layout in TEMPLATE_LAYOUTS:
                if resolved.parts[-len(layout.parts):] == layout.parts:
                    name = candidate
                    break
        if name is None:
            # A deployment is named for the repository that holds it, which is
            # the directory above `adws/`.
            name = resolved.parent.name or resolved.name
    return RuntimeCopy(name=name, root=resolved, kind=kind)


def excluded_relative_paths(
    source: RuntimeCopy, destination: RuntimeCopy
) -> frozenset:
    """Relative paths held out of a comparison or mirror between these two.

    Empty when both endpoints are templates: there `maestro.config.yaml` is
    template-shaped on both sides and `tests/test_template_parity.py` compares
    it like any other file.
    """
    if source.is_template and destination.is_template:
        return frozenset()
    return DEPLOYMENT_OWNED


def sha256_of(path: os.PathLike | str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def scan_runtime(
    root: os.PathLike | str, excluded: Iterable[str] = ()
) -> Dict[str, pathlib.Path]:
    """Map every compared runtime file under ``root`` to its absolute path."""
    base = pathlib.Path(root)
    held_out = frozenset(excluded)
    found: Dict[str, pathlib.Path] = {}
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(
            name for name in dirnames if name not in IGNORED_DIR_NAMES
        )
        for filename in sorted(filenames):
            if filename in IGNORED_FILE_NAMES:
                continue
            if pathlib.PurePath(filename).suffix in IGNORED_SUFFIXES:
                continue
            path = pathlib.Path(dirpath) / filename
            relative = path.relative_to(base).as_posix()
            if relative in held_out:
                continue
            found[relative] = path
    return found


def _line_count(path: os.PathLike | str) -> int:
    try:
        with open(path, "rb") as handle:
            return sum(1 for _ in handle)
    except OSError:
        return -1


@dataclass(frozen=True)
class FileDifference:
    """One file that exists in both copies and holds different bytes."""

    relative_path: str
    source_lines: int
    destination_lines: int

    @property
    def source_is_longer(self) -> bool:
        return self.source_lines > self.destination_lines

    def describe(self, source_name: str, destination_name: str) -> str:
        if self.source_lines == self.destination_lines:
            direction = "same line count ({lines}), differing bytes".format(
                lines=self.source_lines
            )
        elif self.source_is_longer:
            direction = "{a} is ahead by {delta} lines ({a_lines} vs {b_lines})".format(
                a=source_name,
                delta=self.source_lines - self.destination_lines,
                a_lines=self.source_lines,
                b_lines=self.destination_lines,
            )
        else:
            direction = "{b} is ahead by {delta} lines ({b_lines} vs {a_lines})".format(
                b=destination_name,
                delta=self.destination_lines - self.source_lines,
                b_lines=self.destination_lines,
                a_lines=self.source_lines,
            )
        return "{rel}: {direction}".format(rel=self.relative_path, direction=direction)


@dataclass(frozen=True)
class DriftReport:
    """A structured answer to "are these two copies level?".

    The absence fields are separate from :attr:`differing` on purpose. A file
    present in one copy and gone from the other is the loss mode that cost 6,009
    lines, and it is not a content difference; a caller that has to tell them
    apart must not have to parse a message to do it.
    """

    source: RuntimeCopy
    destination: RuntimeCopy
    absent_from_destination: Tuple[str, ...] = ()
    absent_from_source: Tuple[str, ...] = ()
    differing: Tuple[FileDifference, ...] = ()
    excluded: Tuple[str, ...] = ()
    compared: int = 0

    @property
    def is_level(self) -> bool:
        return not (
            self.absent_from_destination or self.absent_from_source or self.differing
        )

    @property
    def missing_files(self) -> Tuple[str, ...]:
        """Every file absent from one side, in either direction."""
        return tuple(sorted(self.absent_from_destination + self.absent_from_source))

    def describe(self, repair: str = "") -> str:
        if self.is_level:
            return "{a} and {b} are level over {n} compared file(s).".format(
                a=self.source.name, b=self.destination.name, n=self.compared
            )
        lines = [
            "The ADW runtime copies disagree: {a} ({a_path}) vs {b} ({b_path}).".format(
                a=self.source.name,
                a_path=self.source.root,
                b=self.destination.name,
                b_path=self.destination.root,
            )
        ]
        missing = self.missing_files
        if missing:
            lines.append(
                "  {n} file(s) exist in one copy and not the other — that is a "
                "deletion, not an edit.".format(n=len(missing))
            )
        if self.absent_from_destination:
            lines.append(
                "  present in {a} but absent from {b}:".format(
                    a=self.source.name, b=self.destination.name
                )
            )
            lines.extend("    " + rel for rel in self.absent_from_destination)
        if self.absent_from_source:
            lines.append(
                "  present in {b} but absent from {a}:".format(
                    a=self.source.name, b=self.destination.name
                )
            )
            lines.extend("    " + rel for rel in self.absent_from_source)
        if self.differing:
            lines.append("  differing content:")
            lines.extend(
                "    " + item.describe(self.source.name, self.destination.name)
                for item in self.differing
            )
        if self.excluded:
            lines.append(
                "  held out (deployment-owned): " + ", ".join(self.excluded)
            )
        if repair:
            lines.append("  " + repair)
        return "\n".join(lines)


def compare(source: RuntimeCopy, destination: RuntimeCopy) -> DriftReport:
    """Compare two runtime copies file by file and report the drift."""
    excluded = excluded_relative_paths(source, destination)
    mine = scan_runtime(source.root, excluded)
    theirs = scan_runtime(destination.root, excluded)

    shared = sorted(set(mine) & set(theirs))
    differing: List[FileDifference] = []
    for relative in shared:
        if mine[relative].read_bytes() == theirs[relative].read_bytes():
            continue
        differing.append(
            FileDifference(
                relative_path=relative,
                source_lines=_line_count(mine[relative]),
                destination_lines=_line_count(theirs[relative]),
            )
        )

    return DriftReport(
        source=source,
        destination=destination,
        absent_from_destination=tuple(sorted(set(mine) - set(theirs))),
        absent_from_source=tuple(sorted(set(theirs) - set(mine))),
        differing=tuple(differing),
        excluded=tuple(sorted(excluded)),
        compared=len(shared),
    )


def copy_verified(source: os.PathLike | str, destination: os.PathLike | str) -> str:
    """Copy one file and prove it arrived. Returns the shared sha256.

    The proof is taken by reading *both* files back after the write rather than
    by trusting the copy's return. Raises :class:`VerificationError` if they
    disagree, which leaves the caller holding a named failure instead of a
    silently short file.
    """
    src = pathlib.Path(source)
    dst = pathlib.Path(destination)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    want = sha256_of(src)
    got = sha256_of(dst)
    if want != got:
        raise VerificationError(
            "COPY_VERIFICATION_FAILED {dst}: expected {want}, found {got}".format(
                dst=dst, want=want, got=got
            )
        )
    return want


@dataclass(frozen=True)
class Refusal:
    """One file the mirror declined to overwrite, and every signal that said so.

    `reasons` is plural because the two signals are independent and either alone
    is enough. Collapsing them to one label would hide the case where mtime says
    nothing — which, on a freshly checked-out source tree, is every case.
    """

    relative_path: str
    reasons: Tuple[str, ...]
    source_mtime_ns: int
    destination_mtime_ns: int
    source_lines: int
    destination_lines: int

    def describe(self) -> str:
        detail = []
        if DESTINATION_NEWER in self.reasons:
            detail.append(
                "destination mtime {dst} > source mtime {src}".format(
                    dst=self.destination_mtime_ns, src=self.source_mtime_ns
                )
            )
        if DESTINATION_LONGER in self.reasons:
            detail.append(
                "destination has {dst} lines to the source's {src}".format(
                    dst=self.destination_lines, src=self.source_lines
                )
            )
        return "{rel}: {reasons} ({detail})".format(
            rel=self.relative_path,
            reasons="+".join(self.reasons),
            detail="; ".join(detail),
        )


@dataclass(frozen=True)
class MirrorResult:
    """What a mirror did, or — when ``applied`` is false — what it would do."""

    source: RuntimeCopy
    destination: RuntimeCopy
    applied: bool
    copied: Tuple[str, ...] = ()
    unchanged: Tuple[str, ...] = ()
    refused: Tuple[Refusal, ...] = ()
    overridden: Tuple[Refusal, ...] = ()
    excluded: Tuple[str, ...] = ()
    left_in_destination: Tuple[str, ...] = ()
    digests: Dict[str, str] = field(default_factory=dict)

    @property
    def is_clean(self) -> bool:
        """True when nothing was refused. A refusal is a result, not a warning."""
        return not self.refused

    def describe(self) -> str:
        verb = "mirrored" if self.applied else "would mirror"
        lines = [
            "{verb} {n} file(s) from {a} to {b}".format(
                verb=verb,
                n=len(self.copied),
                a=self.source.name,
                b=self.destination.name,
            ),
            "  already identical: {n}".format(n=len(self.unchanged)),
        ]
        if self.excluded:
            lines.append(
                "  held out (deployment-owned): " + ", ".join(self.excluded)
            )
        if self.left_in_destination:
            lines.append(
                "  present only in {b}, left untouched: {n}".format(
                    b=self.destination.name, n=len(self.left_in_destination)
                )
            )
            lines.extend("    " + rel for rel in self.left_in_destination)
        if self.overridden:
            lines.append(
                "  OVERWROTE a destination that looked ahead (--overwrite-ahead): "
                "{n}".format(n=len(self.overridden))
            )
            lines.extend("    " + item.describe() for item in self.overridden)
        if self.refused:
            lines.append("  REFUSED, destination is ahead: {n}".format(n=len(self.refused)))
            lines.extend("    " + item.describe() for item in self.refused)
            lines.append(
                "  Reconcile those files by hand, or re-run with "
                "--overwrite-ahead to discard the destination's version."
            )
        return "\n".join(lines)


def mirror(
    source: RuntimeCopy,
    destination: RuntimeCopy,
    *,
    apply: bool = False,
    overwrite_ahead: bool = False,
) -> MirrorResult:
    """Bring ``destination`` level with ``source``. Plans unless ``apply``.

    Never deletes. A file present only in the destination is reported and left
    alone, because deleting it is precisely the loss this module exists to stop.

    A destination file that differs and looks *ahead* — newer by mtime, or
    longer by line count — is refused rather than replaced, unless
    ``overwrite_ahead`` names the discard. Neither signal is proof; between them
    they cover the two shapes the loss has actually taken.
    """
    excluded = excluded_relative_paths(source, destination)
    mine = scan_runtime(source.root, excluded)
    theirs = scan_runtime(destination.root, excluded)

    copied: List[str] = []
    unchanged: List[str] = []
    refused: List[Refusal] = []
    overridden: List[Refusal] = []
    digests: Dict[str, str] = {}

    for relative in sorted(mine):
        src = mine[relative]
        dst = theirs.get(relative, destination.root / relative)
        if dst.exists() and src.read_bytes() == dst.read_bytes():
            unchanged.append(relative)
            continue
        if dst.exists():
            src_mtime = src.stat().st_mtime_ns
            dst_mtime = dst.stat().st_mtime_ns
            src_lines = _line_count(src)
            dst_lines = _line_count(dst)
            signals: List[str] = []
            if dst_mtime > src_mtime:
                signals.append(DESTINATION_NEWER)
            if dst_lines > src_lines:
                signals.append(DESTINATION_LONGER)
            if signals:
                refusal = Refusal(
                    relative_path=relative,
                    reasons=tuple(signals),
                    source_mtime_ns=src_mtime,
                    destination_mtime_ns=dst_mtime,
                    source_lines=src_lines,
                    destination_lines=dst_lines,
                )
                if not overwrite_ahead:
                    refused.append(refusal)
                    continue
                overridden.append(refusal)
        if apply:
            digests[relative] = copy_verified(src, dst)
        copied.append(relative)

    return MirrorResult(
        source=source,
        destination=destination,
        applied=apply,
        copied=tuple(copied),
        unchanged=tuple(unchanged),
        refused=tuple(refused),
        overridden=tuple(overridden),
        excluded=tuple(sorted(excluded)),
        left_in_destination=tuple(sorted(set(theirs) - set(mine))),
        digests=digests,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="runtime_sync",
        description="Compare or mirror ADW runtime copies, with proof.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="report drift between two copies")
    check.add_argument("source")
    check.add_argument("destination")

    push = sub.add_parser("mirror", help="bring a destination level with a source")
    push.add_argument("source")
    push.add_argument("destination")
    push.add_argument(
        "--apply",
        action="store_true",
        help="actually write; without it the run is a plan and touches nothing",
    )
    push.add_argument(
        "--overwrite-ahead",
        action="store_true",
        help="discard destination files that look ahead of the source "
             "(newer by mtime, or longer by line count)",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(list(argv) if argv is not None else None)
    source = describe_copy(args.source)
    destination = describe_copy(args.destination)
    if not source.root.is_dir():
        print("SOURCE_MISSING {}".format(source.root), file=sys.stderr)
        return 2
    if not destination.root.is_dir():
        print("DESTINATION_MISSING {}".format(destination.root), file=sys.stderr)
        return 2

    if args.command == "check":
        report = compare(source, destination)
        print(report.describe())
        return 0 if report.is_level else 1

    result = mirror(
        source,
        destination,
        apply=args.apply,
        overwrite_ahead=args.overwrite_ahead,
    )
    print(result.describe())
    return 0 if result.is_clean else 1


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
