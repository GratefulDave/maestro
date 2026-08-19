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
3. **Deployment-owned configuration never crosses, and what else a deployment
   owns is declared rather than guessed.** `maestro.config.yaml` names a
   particular installation's lane vendors, models, and concurrency, so it is
   excluded from any comparison or mirror where either endpoint is a
   deployment. Between two *template* checkouts it is compared and copied
   normally, because there both copies hold template-shaped values. A
   deployment that carries further surface of its own — a module the template
   has never had — says so in a registry (`load_deployment_registry`), and the
   paths it names come back out of every report in `declared_excluded` rather
   than quietly shrinking the compared set. Nothing is ever inferred into that
   list: a file that stops being compared without saying so is the same defect
   as a file that stops existing without saying so.
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
refuses to encode one.

**It never writes anywhere by itself.** `tests/test_deployment_parity.py` runs
`compare` against every declared deployment on every suite run, so drift there
reports itself instead of waiting for somebody to remember. It does not mirror,
and nothing in this repository does so automatically. A deployment is a live
checkout holding other people's in-flight work: on 2026-08-19 an agent running
ordinary branch hygiene in one of them destroyed a patch with `git restore
--staged --worktree`, and the bytes survived only because an unrelated `git add`
had happened to reach the object store minutes earlier. An unprompted write into
such a repository is that incident with a larger blast radius. Detection is
automatic; the write stays a command a person types.

It also says nothing about whether a copy is committed:
bytes agreeing on disk is not bytes agreeing in git, and a checkout whose runtime
is untracked can be rewritten without leaving history no matter what this reports.

Run:  uv run adw_test.py -k runtime_sync
"""

from __future__ import annotations

import argparse
import hashlib
import json
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

#: Where the list of deployments to watch is declared, and the only two places
#: it is looked for.
#:
#: The list is *not* part of the runtime and is deliberately not shipped inside
#: it. It names absolute or checkout-relative paths on one machine, so a copy
#: committed into the template would be wrong in every checkout but the one it
#: was written in, and would then have to be identical in the-library for
#: `tests/test_template_parity.py` to pass. It lives beside the repository
#: instead, at `<repository>/.maestro/deployments.json`, or wherever
#: ``MAESTRO_DEPLOYMENT_REGISTRY`` points.
#:
#: Its absence is the ordinary case. A machine with no deployments installed —
#: CI, a fresh clone, anyone else's laptop — has no registry, and the
#: deployment check skips there rather than failing. Watching a deployment is
#: opt-in by writing this file, never by a path hardcoded in a test.
DEPLOYMENT_REGISTRY_ENV = "MAESTRO_DEPLOYMENT_REGISTRY"
DEPLOYMENT_REGISTRY_RELATIVE = pathlib.PurePosixPath(".maestro/deployments.json")

#: Keys a registry entry may carry. Anything else is refused by name rather
#: than ignored: a misspelled `pinned` that reads as "no exclusions" would put
#: a deployment's own files back into the comparison, and a misspelled `root`
#: would silently watch nothing. Both failures are quiet, which is the one
#: property this module exists to remove.
REGISTRY_ENTRY_KEYS = frozenset({"name", "root", "pinned", "note"})

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
    """One checkout's ADW runtime directory, and what kind of copy it is.

    ``pinned`` is what this copy declares it owns outright: relative paths held
    out of any comparison or mirror this copy takes part in, in either
    direction. It is empty for a template — the shipped runtime owns nothing
    privately — and is populated for a deployment from the registry that
    declares that deployment (see :func:`load_deployment_registry`).

    An exclusion is the mechanism by which drift becomes invisible, so the
    declaration is carried on the copy rather than passed loose at the call
    site, and every held-out path comes back out in the report's
    ``declared_excluded``. A path that stops being compared without saying so
    is the same defect as a file that stops existing without saying so.
    """

    name: str
    root: pathlib.Path
    kind: str
    pinned: Tuple[str, ...] = ()

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


def describe_copy(
    root: os.PathLike | str,
    name: Optional[str] = None,
    pinned: Iterable[str] = (),
    kind: Optional[str] = None,
) -> RuntimeCopy:
    """Build a :class:`RuntimeCopy` for ``root``, classifying it by path shape.

    ``kind`` overrides that classification, which a registry entry uses: a
    deployment installed at a path that happens to match a template layout is
    still a deployment, and reading it as a template would let its
    ``maestro.config.yaml`` cross.
    """
    resolved = pathlib.Path(root).resolve()
    kind = kind or classify(resolved)
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
    return RuntimeCopy(
        name=name, root=resolved, kind=kind, pinned=tuple(sorted(set(pinned)))
    )


def declared_exclusions(source: RuntimeCopy, destination: RuntimeCopy) -> frozenset:
    """Paths either copy declares it owns. Never inferred, always declared."""
    return frozenset(source.pinned) | frozenset(destination.pinned)


def excluded_relative_paths(
    source: RuntimeCopy, destination: RuntimeCopy
) -> frozenset:
    """Relative paths held out of a comparison or mirror between these two.

    Two sources, and they are not the same kind of thing.

    ``DEPLOYMENT_OWNED`` is implicit and hardcoded: `maestro.config.yaml` names
    one installation's lane vendors, models and concurrency, and holding it back
    needs no per-deployment declaration. It applies only when a deployment is
    one of the endpoints; between two *template* checkouts it is
    template-shaped on both sides and `tests/test_template_parity.py` compares
    it like any other file.

    Everything else is *declared* — `RuntimeCopy.pinned`, populated from a
    deployment registry a person wrote. That is the answer to a deployment
    carrying its own surface: `adw_modules/deliver.py` and
    `adw_modules/finalization_window.py` do not exist in the template at all in
    one installation, so refusing them on every mirror is noise rather than
    signal. Declaring them stops the noise; nothing infers them, because an
    exclusion that appears on its own is how a drifted file stops being
    watched.
    """
    declared = declared_exclusions(source, destination)
    if source.is_template and destination.is_template:
        return declared
    return declared | DEPLOYMENT_OWNED


class RegistryError(ValueError):
    """A deployment registry exists and cannot be read as one.

    Distinct from the registry being absent, which is normal and skips. A
    registry that is present and malformed must never degrade to "no
    deployments declared": that would turn a typo into a check that watches
    nothing and says so nowhere.
    """


@dataclass(frozen=True)
class DeploymentEntry:
    """One deployed instance a person has asked to be watched.

    ``pinned`` holds the paths that installation owns — files that are its own
    surface rather than drifted runtime, and so are not part of the comparison
    at all. Declaring one is a judgement about two divergent copies, which is
    why it is written down by hand and why it comes back out in every report
    (:attr:`DriftReport.declared_excluded`) rather than quietly shrinking the
    compared set.
    """

    name: str
    root: pathlib.Path
    pinned: Tuple[str, ...] = ()
    note: str = ""

    def as_copy(self) -> RuntimeCopy:
        """This entry as a :class:`RuntimeCopy`, always of kind DEPLOYMENT.

        The kind is asserted rather than classified. A deployment installed at
        a path that happens to end in a template layout is still a deployment,
        and reading it as a template would let `maestro.config.yaml` cross.
        """
        return describe_copy(
            self.root, name=self.name, pinned=self.pinned, kind=DEPLOYMENT
        )


def repository_root_of(root: os.PathLike | str) -> Optional[pathlib.Path]:
    """The repository holding ``root``, when ``root`` is a template runtime.

    None for a deployment, whose enclosing repository is not derivable from the
    layout: a deployment is installed wherever it was installed.
    """
    resolved = pathlib.Path(root).resolve()
    for _name, layout in sorted(
        TEMPLATE_LAYOUTS, key=lambda item: len(item[1].parts), reverse=True
    ):
        parts = layout.parts
        if resolved.parts[-len(parts):] == parts:
            return resolved.parents[len(parts) - 1]
    return None


def registry_search_paths(
    repo_roots: Iterable[os.PathLike | str] = (),
    environ: Optional[Dict[str, str]] = None,
) -> Tuple[pathlib.Path, ...]:
    """Every path a deployment registry is looked for, in order.

    ``MAESTRO_DEPLOYMENT_REGISTRY`` wins outright when set — an explicit answer
    must not be silently supplemented by a default one — and a value that
    points at nothing is reported as absent by the caller, naming the path, so
    a typo in it reads as "looked here, found nothing" rather than as "no
    deployments exist".
    """
    env = os.environ if environ is None else environ
    override = env.get(DEPLOYMENT_REGISTRY_ENV, "").strip()
    if override:
        return (pathlib.Path(override).expanduser(),)
    found: List[pathlib.Path] = []
    for root in repo_roots:
        candidate = pathlib.Path(root) / DEPLOYMENT_REGISTRY_RELATIVE
        if candidate not in found:
            found.append(candidate)
    return tuple(found)


def _registry_entry(raw, path: pathlib.Path, index: int) -> DeploymentEntry:
    where = "{path} entry {index}".format(path=path, index=index)
    if not isinstance(raw, dict):
        raise RegistryError(
            "REGISTRY_ENTRY_NOT_AN_OBJECT {where}: found {kind}".format(
                where=where, kind=type(raw).__name__
            )
        )
    unknown = sorted(set(raw) - REGISTRY_ENTRY_KEYS)
    if unknown:
        raise RegistryError(
            "REGISTRY_UNKNOWN_KEY {where}: {keys}; known keys are {known}".format(
                where=where,
                keys=", ".join(unknown),
                known=", ".join(sorted(REGISTRY_ENTRY_KEYS)),
            )
        )
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise RegistryError(
            "REGISTRY_MISSING_NAME {where}: every deployment needs a name".format(
                where=where
            )
        )
    root = raw.get("root")
    if not isinstance(root, str) or not root.strip():
        raise RegistryError(
            "REGISTRY_MISSING_ROOT {where}: every deployment needs a root".format(
                where=where
            )
        )
    resolved = pathlib.Path(root).expanduser()
    if not resolved.is_absolute():
        # Relative to the registry file, so a registry can name a sibling
        # checkout without hardcoding one machine's home directory.
        resolved = (path.parent / resolved).resolve()

    pinned_raw = raw.get("pinned", [])
    if not isinstance(pinned_raw, list):
        raise RegistryError(
            "REGISTRY_PINNED_NOT_A_LIST {where}: found {kind}".format(
                where=where, kind=type(pinned_raw).__name__
            )
        )
    pinned: List[str] = []
    for item in pinned_raw:
        if not isinstance(item, str) or not item.strip():
            raise RegistryError(
                "REGISTRY_PINNED_NOT_A_PATH {where}: {item!r}".format(
                    where=where, item=item
                )
            )
        candidate = pathlib.PurePosixPath(item)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise RegistryError(
                "REGISTRY_PINNED_NOT_RELATIVE {where}: {item!r} must be a path "
                "inside the runtime, relative to its root".format(
                    where=where, item=item
                )
            )
        pinned.append(candidate.as_posix())

    note = raw.get("note", "")
    if not isinstance(note, str):
        raise RegistryError(
            "REGISTRY_NOTE_NOT_TEXT {where}: found {kind}".format(
                where=where, kind=type(note).__name__
            )
        )
    return DeploymentEntry(
        name=name.strip(),
        root=resolved,
        pinned=tuple(sorted(set(pinned))),
        note=note,
    )


def load_deployment_registry(
    path: os.PathLike | str,
) -> Tuple[DeploymentEntry, ...]:
    """Read the declared deployments from ``path``.

    Every malformation raises :class:`RegistryError` naming what was wrong and
    where. Nothing here degrades to a default.
    """
    path = pathlib.Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise RegistryError(
            "REGISTRY_UNREADABLE {path}: {error}".format(path=path, error=error)
        )
    try:
        document = json.loads(text)
    except json.JSONDecodeError as error:
        raise RegistryError(
            "REGISTRY_NOT_JSON {path}: {error}".format(path=path, error=error)
        )
    if not isinstance(document, dict):
        raise RegistryError(
            "REGISTRY_NOT_AN_OBJECT {path}: the top level must be an object with "
            'a "deployments" list'.format(path=path)
        )
    unknown = sorted(set(document) - {"deployments"})
    if unknown:
        raise RegistryError(
            "REGISTRY_UNKNOWN_KEY {path}: {keys}".format(
                path=path, keys=", ".join(unknown)
            )
        )
    declared = document.get("deployments")
    if not isinstance(declared, list):
        raise RegistryError(
            'REGISTRY_MISSING_DEPLOYMENTS {path}: "deployments" must be a '
            "list".format(path=path)
        )

    entries = tuple(
        _registry_entry(raw, path, index) for index, raw in enumerate(declared)
    )
    seen: Dict[str, int] = {}
    for index, entry in enumerate(entries):
        if entry.name in seen:
            raise RegistryError(
                "REGISTRY_DUPLICATE_NAME {path}: {name} is declared twice "
                "(entries {first} and {second})".format(
                    path=path, name=entry.name, first=seen[entry.name], second=index
                )
            )
        seen[entry.name] = index
    return entries


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
    declared_excluded: Tuple[str, ...] = ()
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

    @property
    def source_ahead(self) -> Tuple[FileDifference, ...]:
        """Differing files the source holds strictly more lines of.

        Direction matters because the two directions call for opposite actions.
        A destination behind its source is repaired by mirroring. A destination
        *ahead* of its source holds work that exists in one copy only, which no
        mechanism may reconcile in either direction without a person reading
        both — the situation issue #71 records for twelve files.
        """
        return tuple(item for item in self.differing if item.source_is_longer)

    @property
    def destination_ahead(self) -> Tuple[FileDifference, ...]:
        """Differing files the destination holds strictly more lines of."""
        return tuple(
            item
            for item in self.differing
            if item.destination_lines > item.source_lines
        )

    @property
    def undetermined_direction(self) -> Tuple[FileDifference, ...]:
        """Differing files with equal line counts, so neither side is shown ahead.

        Line count is the only content signal here, and it says nothing when the
        counts match. These are reported with the destination-ahead finding
        rather than the source-ahead one, because "we cannot show the source is
        newer" is a reason for a person to read both copies and never a licence
        to overwrite.
        """
        return tuple(
            item
            for item in self.differing
            if item.source_lines == item.destination_lines
        )

    def _exclusion_lines(self) -> List[str]:
        """Say what was held out, on a level report as much as a drifted one.

        A held-out path is a file nothing is watching. Printing it only when
        something else already failed would make the quiet case — everything
        level, three files silently outside the comparison — the one case where
        the exclusion never appears.
        """
        lines: List[str] = []
        implicit = tuple(
            rel for rel in self.excluded if rel not in set(self.declared_excluded)
        )
        if implicit:
            lines.append(
                "  held out (deployment-owned): " + ", ".join(implicit)
            )
        if self.declared_excluded:
            lines.append(
                "  held out ({b} declares it owns these): {paths}".format(
                    b=self.destination.name
                    if self.destination.pinned
                    else self.source.name,
                    paths=", ".join(self.declared_excluded),
                )
            )
        return lines

    def describe(self, repair: str = "") -> str:
        if self.is_level:
            level = ["{a} and {b} are level over {n} compared file(s).".format(
                a=self.source.name, b=self.destination.name, n=self.compared
            )]
            level.extend(self._exclusion_lines())
            return "\n".join(level)
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
        lines.extend(self._exclusion_lines())
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
        declared_excluded=tuple(sorted(declared_exclusions(source, destination))),
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
    declared_excluded: Tuple[str, ...] = ()
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
        implicit = tuple(
            rel for rel in self.excluded if rel not in set(self.declared_excluded)
        )
        if implicit:
            lines.append("  held out (deployment-owned): " + ", ".join(implicit))
        if self.declared_excluded:
            lines.append(
                "  held out (declared deployment-owned): "
                + ", ".join(self.declared_excluded)
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
        declared_excluded=tuple(sorted(declared_exclusions(source, destination))),
        left_in_destination=tuple(sorted(set(theirs) - set(mine))),
        digests=digests,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="runtime_sync",
        description="Compare or mirror ADW runtime copies, with proof.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_pin(target: argparse.ArgumentParser) -> None:
        target.add_argument(
            "--pin",
            action="append",
            default=[],
            metavar="RELATIVE_PATH",
            help="a path the destination owns outright: held out of the "
                 "comparison in both directions, and named in the report. "
                 "Repeatable. This is the flag form of a deployment registry's "
                 "`pinned` list, so a failure from the deployment check can be "
                 "reproduced verbatim.",
        )

    check = sub.add_parser("check", help="report drift between two copies")
    check.add_argument("source")
    check.add_argument("destination")
    add_pin(check)

    declared = sub.add_parser(
        "check-deployments",
        help="report drift between a template and every declared deployment",
    )
    declared.add_argument("source")
    declared.add_argument(
        "--registry",
        default=None,
        help="the deployment registry to read; defaults to "
             "$" + DEPLOYMENT_REGISTRY_ENV + ", then "
             "<repository>/" + DEPLOYMENT_REGISTRY_RELATIVE.as_posix(),
    )

    push = sub.add_parser("mirror", help="bring a destination level with a source")
    push.add_argument("source")
    push.add_argument("destination")
    add_pin(push)
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


def check_deployments(
    source: RuntimeCopy,
    entries: Sequence[DeploymentEntry],
    write=print,
) -> int:
    """Report drift between ``source`` and every declared deployment.

    The on-demand half of the answer to "nothing runs this for the
    deployments". `tests/test_deployment_parity.py` runs the same comparison on
    every suite run and fails; this prints it when an operator asks. Neither
    writes: a deployment is a live checkout with other people's work in it, and
    the mirror stays a command a person types after reading what this said.

    Returns 0 only when every declared deployment that is installed here is
    level. A deployment that is not on this machine is reported and does not
    count against that, because a deployment nobody installed is not drift.
    """
    worst = 0
    for entry in entries:
        if not entry.root.is_dir():
            write(
                "{name}: not installed at {root}, nothing to compare".format(
                    name=entry.name, root=entry.root
                )
            )
            continue
        report = compare(source, entry.as_copy())
        write(report.describe())
        if report.is_level:
            continue
        worst = 1
        write(
            "  {behind} file(s) where {a} is ahead, {ahead} where {b} is, "
            "{flat} differing with equal line counts, {gone} absent from one "
            "copy.".format(
                a=source.name,
                b=entry.name,
                behind=len(report.source_ahead),
                ahead=len(report.destination_ahead),
                flat=len(report.undetermined_direction),
                gone=len(report.missing_files),
            )
        )
        if report.source_ahead or report.absent_from_destination:
            write(
                "  {name} is running older runtime. Repair it with: "
                "runtime_sync.py mirror {src} {dst} --apply".format(
                    name=entry.name, src=source.root, dst=entry.root
                )
            )
        if (
            report.destination_ahead
            or report.undetermined_direction
            or report.absent_from_source
        ):
            write(
                "  {name} holds work the template does not, or work whose "
                "direction cannot be established. Reconcile it by hand — "
                "decide per file whether it belongs upstream, is local "
                "divergence to discard explicitly with --overwrite-ahead, or "
                "is owned by {name} and belongs in its registry entry's "
                "`pinned` list.".format(name=entry.name)
            )
    return worst


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(list(argv) if argv is not None else None)
    source = describe_copy(args.source)
    if not source.root.is_dir():
        print("SOURCE_MISSING {}".format(source.root), file=sys.stderr)
        return 2

    if args.command == "check-deployments":
        if args.registry:
            candidates: Tuple[pathlib.Path, ...] = (pathlib.Path(args.registry),)
        else:
            repository = repository_root_of(source.root)
            candidates = registry_search_paths(
                [repository] if repository is not None else []
            )
        for candidate in candidates:
            if candidate.is_file():
                return check_deployments(source, load_deployment_registry(candidate))
        print(
            "REGISTRY_MISSING: looked for a deployment registry at {paths}".format(
                paths=", ".join(str(path) for path in candidates) or "(nowhere)"
            ),
            file=sys.stderr,
        )
        return 2

    destination = describe_copy(args.destination, pinned=args.pin)
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
