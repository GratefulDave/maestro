#!/usr/bin/env python3
"""Reclaim dead scratch worktrees from a maestro installation.

A run leaves two kinds of directory under ``<runtime_state_root>/worktrees``.

``<run_id>/<lane_id>/<role>/checkout`` is a worktree of the **target
repository**. It holds the candidate a builder is working on and can carry
uncommitted work, so it is durable for as long as its run is.

``draft-<lane_id>-<nonce>``, ``review-<lane_id>-<digest>``,
``review-base-<lane_id>-<digest>`` and ``integration-gate-<lane_id>-<digest>``
are scratch. Each is rebuilt on demand from an immutable commit plus sealed
blobs, so removing one costs a re-provision and never data.
Nothing removes them today -- not on merge, not on run completion, not on
scheduler exit -- so one installation accumulates them for the life of a plan.

This tool classifies every directory under that root and, only when told to,
removes the ones that are provably derived or provably orphaned. It is a
reclaimer, not a lifecycle actor: it never writes the ledger, the vault
contents, receipts, artifacts, locks, plans, or the target repository's working
tree. It does unregister a worktree it is about to delete, because leaving a
repository with a registration pointing at a missing directory is worse than
leaving the directory.

Dry run is the default. ``--apply`` is the only thing that deletes.

    gc_worktrees.py                       # every installation, dry run
    gc_worktrees.py --state <path>        # one installation, dry run
    gc_worktrees.py --verbose             # list every path, not just totals
    gc_worktrees.py --apply               # actually reclaim

Flags:

    --state PATH        one installation's runtime state root, instead of
                        every installation named in the registry
    --registry PATH     registry to resolve installations from
                        (default ~/.maestro/registry.json)
    --keep N            keep the N most recent scratch trees per lane and kind
                        (default 2) so a post-mortem is still possible
    --all               take those too; the keep window is the only thing this
                        overrides, never a liveness guard
    --min-age-hours H   never touch anything modified more recently than this
                        (default 1), because a scratch tree a running review is
                        executing in looks exactly like a dead one
    --apply             delete; without it nothing is removed
    --verbose           print every path rather than per-kind totals
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Sequence

DEFAULT_REGISTRY = Path.home() / ".maestro" / "registry.json"
DEFAULT_KEEP = 2
DEFAULT_MIN_AGE_HOURS = 1.0

#: A run directory is named for its run id. Both the bare form the current
#: runtime emits and the ``run-`` prefixed form older installations used.
RUN_ID_RE = re.compile(r"^(?:run-)?[0-9a-f]{32}$")

#: The trailing token of a scratch directory name: an input digest prefix or a
#: nonce. Anything else means the name was not built by the factory and the
#: directory is left alone.
SCRATCH_SUFFIX_RE = re.compile(r"^[0-9a-f]{8,}$")

#: Longest first: ``review-base-`` must not be read as ``review-`` over a lane
#: called ``base-...``.
SCRATCH_PREFIXES = (
    "review-base-",
    "review-",
    "draft-",
    "integration-gate-",
)

#: The kinds the recency window applies to, derived so a new prefix cannot be
#: added in one place and silently missed in the other.
SCRATCH_KINDS = frozenset(prefix.rstrip("-") for prefix in SCRATCH_PREFIXES)

#: The stage every lane of a finished run sits in.
MERGED = "MERGED"

#: The run artifact whose existence means the run published.
MAIN_PUBLICATION = "MAIN_PUBLICATION"

#: A registry entry pointing at a state root that no longer exists.
ABSENT_STATE = "state root is absent"


class GcRefused(Exception):
    """A path or an installation this tool will not act on."""


class LedgerUnreadable(Exception):
    """The ledger could not answer which runs exist. Every run tree is kept."""


# --------------------------------------------------------------------------
# installations


@dataclass(frozen=True)
class Installation:
    """One deployment's runtime state root and what it is bound to."""

    state: Path
    database: Path
    repository: Path | None

    @property
    def worktrees(self) -> Path:
        return self.state / "worktrees"

    @property
    def vaults(self) -> Path:
        return self.state / "vaults"


def installation_for_state(state: Path, repository: Path | None = None) -> Installation:
    state = Path(state).expanduser()
    return Installation(
        state=state,
        database=state / "lifecycle.sqlite3",
        repository=Path(repository).expanduser() if repository else None,
    )


def read_registry(path: Path) -> tuple[Installation, ...]:
    """Installations named by the registry, deduplicated by state root.

    A registry that cannot be parsed yields nothing rather than raising: the
    caller can still be pointed at one installation with ``--state``.
    """
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ()
    entries = raw.get("installations") if isinstance(raw, Mapping) else None
    if not isinstance(entries, Sequence):
        return ()
    seen: dict[Path, Installation] = {}
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        state = entry.get("state")
        if not isinstance(state, str) or not state:
            continue
        resolved = Path(state).expanduser()
        if resolved in seen:
            continue
        database = entry.get("database")
        repository = entry.get("repository")
        seen[resolved] = Installation(
            state=resolved,
            database=(
                Path(database).expanduser()
                if isinstance(database, str) and database
                else resolved / "lifecycle.sqlite3"
            ),
            repository=(
                Path(repository).expanduser()
                if isinstance(repository, str) and repository
                else None
            ),
        )
    return tuple(seen.values())


# --------------------------------------------------------------------------
# the ledger


@dataclass(frozen=True)
class RunRow:
    run_id: str
    terminal: bool
    repository: Path | None


def read_runs(database: Path) -> dict[str, RunRow]:
    """Which runs the ledger knows, and whether each one is finished.

    Terminal here is deliberately narrower than ``derive_run_status``: a run is
    finished only when every one of its lanes reached ``MERGED`` *and* a
    ``MAIN_PUBLICATION`` artifact exists. The real derivation additionally binds
    the publication to the active final-review fingerprint, which needs the
    target repository's git state; this tool must classify installations whose
    repository has moved or gone, so it asks a question the ledger alone can
    answer. Both extra conditions can only move a run from finished to
    unfinished, and unfinished means keep.

    Raises `LedgerUnreadable` for anything it cannot answer.
    """
    database = Path(database)
    if not database.is_file():
        raise LedgerUnreadable("no ledger at {0}".format(database))
    uri = "file:{0}?mode=ro".format(database.as_posix().replace("?", "%3f"))
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    except sqlite3.Error as exc:
        raise LedgerUnreadable(str(exc)) from exc
    try:
        try:
            # Older installations predate `target_repository_root`. The column
            # only feeds worktree unregistration, so its absence costs the prune
            # step a repository to ask, never a classification.
            columns = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(runs)").fetchall()
            }
            if "target_repository_root" in columns:
                run_rows = conn.execute(
                    "SELECT run_id, target_repository_root FROM runs"
                ).fetchall()
            else:
                run_rows = [
                    (row[0], None)
                    for row in conn.execute("SELECT run_id FROM runs").fetchall()
                ]
            stage_rows = conn.execute("SELECT run_id, stage FROM lane_state").fetchall()
            published_rows = conn.execute(
                "SELECT DISTINCT run_id FROM run_artifacts WHERE artifact_kind=?",
                (MAIN_PUBLICATION,),
            ).fetchall()
        except sqlite3.Error as exc:
            raise LedgerUnreadable(str(exc)) from exc
    finally:
        conn.close()

    stages: dict[str, list[str]] = {}
    for run_id, stage in stage_rows:
        stages.setdefault(str(run_id), []).append(str(stage))
    published = {str(row[0]) for row in published_rows}

    runs: dict[str, RunRow] = {}
    for run_id, repository in run_rows:
        run_id = str(run_id)
        lane_stages = stages.get(run_id, [])
        terminal = bool(
            run_id in published
            and lane_stages
            and all(stage == MERGED for stage in lane_stages)
        )
        runs[run_id] = RunRow(
            run_id=run_id,
            terminal=terminal,
            repository=Path(str(repository)) if repository else None,
        )
    return runs


# --------------------------------------------------------------------------
# path safety


def assert_within(root: Path, candidate: Path) -> Path:
    """The candidate's real location, or a refusal.

    Both sides are fully resolved before comparison, so a symlink whose target
    lies outside the root is refused even when it is handed in directly. The
    root itself is refused: this tool removes entries under the worktrees
    directory, never the directory.
    """
    real_root = Path(root).resolve(strict=False)
    real = Path(candidate).resolve(strict=False)
    if real == real_root:
        raise GcRefused("refusing the worktrees root itself: {0}".format(real))
    if real_root not in real.parents:
        raise GcRefused(
            "refusing {0}: outside {1}".format(candidate, real_root)
        )
    return real


def directory_size(path: Path) -> int:
    """Bytes occupied by a directory tree, not following symlinks."""
    total = 0
    stack = [Path(path)]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                    total += entry.stat(follow_symlinks=False).st_size
                else:
                    total += entry.stat(follow_symlinks=False).st_size
            except OSError:
                continue
    try:
        total += Path(path).lstat().st_size
    except OSError:
        pass
    return total


# --------------------------------------------------------------------------
# classification

RECLAIM = "reclaim"
KEEP = "keep"


@dataclass(frozen=True)
class Entry:
    path: Path
    kind: str
    group: str
    size: int
    mtime: float
    decision: str
    reason: str


@dataclass
class Plan:
    installation: Installation
    entries: list[Entry] = field(default_factory=list)
    note: str = ""

    @property
    def reclaimable(self) -> list[Entry]:
        return [entry for entry in self.entries if entry.decision == RECLAIM]

    @property
    def kept(self) -> list[Entry]:
        return [entry for entry in self.entries if entry.decision == KEEP]


def scratch_group(name: str) -> tuple[str, str] | None:
    """``(kind, group)`` for a scratch directory name, or None.

    The group is the kind plus the lane id, so the keep-most-recent window is
    per lane and per kind rather than global.
    """
    for prefix in SCRATCH_PREFIXES:
        if not name.startswith(prefix):
            continue
        rest = name[len(prefix) :]
        lane, sep, suffix = rest.rpartition("-")
        if not sep or not lane or not SCRATCH_SUFFIX_RE.match(suffix):
            return None
        return prefix.rstrip("-"), "{0}{1}".format(prefix, lane)
    return None


def draft_vault_run_id(entry: Path, state: Path) -> str | None:
    """The run whose vault a draft tree is still registered in, if any.

    A draft worktree records its git directory as
    ``<state>/vaults/<run_id>.git/worktrees/<name>``. That directory existing is
    git's own registration record, so its presence answers both questions --
    which run, and still registered -- without shelling out.
    """
    marker = Path(entry) / ".git"
    try:
        text = marker.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text.startswith("gitdir:"):
        return None
    gitdir = Path(text[len("gitdir:") :].strip())
    if not gitdir.is_absolute():
        gitdir = (Path(entry) / gitdir).resolve(strict=False)
    if not gitdir.is_dir():
        return None
    vaults = Path(state).resolve(strict=False) / "vaults"
    try:
        relative = gitdir.resolve(strict=False).relative_to(vaults)
    except ValueError:
        return None
    if not relative.parts:
        return None
    vault_name = relative.parts[0]
    if not vault_name.endswith(".git"):
        return None
    return vault_name[: -len(".git")]


def classify(
    installation: Installation,
    runs: Mapping[str, RunRow],
    *,
    keep: int = DEFAULT_KEEP,
    take_all: bool = False,
    min_age_s: float = DEFAULT_MIN_AGE_HOURS * 3600.0,
    now: float | None = None,
) -> Plan:
    """Decide, for every directory under the worktrees root, keep or reclaim.

    Unknown means keep. A name this tool does not recognise, a symlink, a run
    that is not finished, and anything modified inside the liveness window are
    all kept.
    """
    plan = Plan(installation=installation)
    now = time.time() if now is None else now
    root = installation.worktrees
    if not root.is_dir():
        plan.note = "no worktrees directory"
        return plan
    real_root = root.resolve(strict=False)

    candidates: list[Entry] = []
    with os.scandir(root) as scan:
        children = sorted(scan, key=lambda item: item.name)
    for child in children:
        path = Path(child.path)
        name = child.name
        if child.is_symlink():
            candidates.append(
                _entry(path, "symlink", name, KEEP, "symlink; not classified")
            )
            continue
        if not child.is_dir(follow_symlinks=False):
            candidates.append(
                _entry(path, "file", name, KEEP, "not a directory")
            )
            continue
        try:
            assert_within(real_root, path)
        except GcRefused as exc:
            candidates.append(_entry(path, "escaped", name, KEEP, str(exc)))
            continue

        scratch = scratch_group(name)
        if scratch is not None:
            kind, group = scratch
            candidates.append(
                _scratch_entry(installation, path, kind, group, runs)
            )
            continue

        if RUN_ID_RE.match(name):
            run_id = name[len("run-") :] if name.startswith("run-") else name
            row = runs.get(run_id) or runs.get(name)
            if row is None:
                candidates.append(
                    _entry(
                        path,
                        "orphan-run",
                        name,
                        RECLAIM,
                        "run not in ledger",
                    )
                )
            elif row.terminal:
                candidates.append(
                    _entry(path, "run", name, RECLAIM, "run is finished")
                )
            else:
                candidates.append(
                    _entry(path, "run", name, KEEP, "run is not finished")
                )
            continue

        candidates.append(
            _entry(path, "unknown", name, KEEP, "unrecognised name")
        )

    plan.entries = _apply_windows(
        candidates, keep=keep, take_all=take_all, min_age_s=min_age_s, now=now
    )
    return plan


def _entry(path: Path, kind: str, group: str, decision: str, reason: str) -> Entry:
    try:
        mtime = path.lstat().st_mtime
    except OSError:
        mtime = 0.0
    size = directory_size(path) if decision == RECLAIM else 0
    return Entry(
        path=path,
        kind=kind,
        group=group,
        size=size,
        mtime=mtime,
        decision=decision,
        reason=reason,
    )


def _scratch_entry(
    installation: Installation,
    path: Path,
    kind: str,
    group: str,
    runs: Mapping[str, RunRow],
) -> Entry:
    """A scratch tree is derived, except while it is anchoring an unpinned commit.

    ``write_test_draft`` keeps a draft worktree registered until the draft ref is
    pinned, because the worktree's HEAD is the only thing referencing the commit
    until then. A registered draft belonging to a run that is still going is
    therefore not derived, and is kept under every flag.
    """
    if kind == "draft":
        run_id = draft_vault_run_id(path, installation.state)
        if run_id is not None:
            row = runs.get(run_id)
            if row is not None and not row.terminal:
                return _entry(
                    path,
                    kind,
                    group,
                    KEEP,
                    "registered in the vault of a live run",
                )
    return _entry(path, kind, group, RECLAIM, "derived scratch tree")


def _apply_windows(
    candidates: Sequence[Entry],
    *,
    keep: int,
    take_all: bool,
    min_age_s: float,
    now: float,
) -> list[Entry]:
    """Demote to KEEP anything inside the recency window or the liveness window."""
    protected: set[Path] = set()
    if not take_all and keep > 0:
        groups: dict[str, list[Entry]] = {}
        for entry in candidates:
            if entry.decision != RECLAIM or entry.kind not in SCRATCH_KINDS:
                continue
            groups.setdefault(entry.group, []).append(entry)
        for members in groups.values():
            members.sort(key=lambda item: item.mtime, reverse=True)
            protected.update(item.path for item in members[:keep])

    resolved: list[Entry] = []
    for entry in candidates:
        if entry.decision != RECLAIM:
            resolved.append(entry)
            continue
        if entry.path in protected:
            resolved.append(
                Entry(
                    path=entry.path,
                    kind=entry.kind,
                    group=entry.group,
                    size=0,
                    mtime=entry.mtime,
                    decision=KEEP,
                    reason="within the most-recent-{0} window".format(keep),
                )
            )
            continue
        if min_age_s > 0 and (now - entry.mtime) < min_age_s:
            resolved.append(
                Entry(
                    path=entry.path,
                    kind=entry.kind,
                    group=entry.group,
                    size=0,
                    mtime=entry.mtime,
                    decision=KEEP,
                    reason="modified within the liveness window",
                )
            )
            continue
        resolved.append(entry)
    return resolved


# --------------------------------------------------------------------------
# removal


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def registered_worktrees(repo: Path) -> dict[Path, Path]:
    """``{resolved worktree path: repo}`` for every worktree the repo knows."""
    repo = Path(repo)
    if not repo.exists():
        return {}
    result = _git(repo, "worktree", "list", "--porcelain")
    if result.returncode != 0:
        return {}
    found: dict[Path, Path] = {}
    for line in (result.stdout or "").splitlines():
        if not line.startswith("worktree "):
            continue
        found[Path(line[len("worktree ") :].strip()).resolve(strict=False)] = repo
    return found


def registration_index(
    installation: Installation, runs: Mapping[str, RunRow]
) -> dict[Path, Path]:
    """Every worktree registration that could point into this state root.

    Both owners are consulted: the target repository, which owns the per-role
    checkouts, and each run's vault, which owns the draft trees.
    """
    index: dict[Path, Path] = {}
    repos: list[Path] = []
    if installation.repository is not None:
        repos.append(installation.repository)
    for row in runs.values():
        if row.repository is not None:
            repos.append(row.repository)
    for repo in dict.fromkeys(Path(item) for item in repos):
        index.update(registered_worktrees(repo))
    vaults = installation.vaults
    if vaults.is_dir():
        with os.scandir(vaults) as scan:
            for child in scan:
                if child.name.endswith(".git"):
                    index.update(registered_worktrees(Path(child.path)))
    return index


def remove_entry(
    entry: Entry, *, worktrees_root: Path, index: Mapping[Path, Path]
) -> int:
    """Unregister anything git knows about under this path, then delete it.

    Returns the bytes measured for the entry. Refuses -- loudly -- any path that
    does not resolve underneath the worktrees root, so a symlinked entry can
    never lead the deletion out of the tree.
    """
    real = assert_within(worktrees_root, entry.path)
    owners: set[Path] = set()
    for registered, repo in index.items():
        if registered == real or real in registered.parents:
            owners.add(repo)
            _git(repo, "worktree", "remove", "--force", str(registered))
    if real.is_dir():
        shutil.rmtree(real, ignore_errors=True)
    for repo in owners:
        _git(repo, "worktree", "prune")
    return entry.size


# --------------------------------------------------------------------------
# reporting


def human(size: int) -> str:
    value = float(size)
    for unit in ("B", "K", "M", "G", "T"):
        if value < 1024.0 or unit == "T":
            if unit == "B":
                return "{0:.0f}B".format(value)
            return "{0:.1f}{1}".format(value, unit)
        value /= 1024.0
    return "{0:.1f}T".format(value)


def render(plans: Sequence[Plan], *, applied: bool, verbose: bool) -> str:
    lines: list[str] = []
    total_dirs = 0
    total_bytes = 0
    absent = 0
    verb = "reclaimed" if applied else "would reclaim"
    for plan in plans:
        # A registry accumulates one entry per throwaway test installation, and
        # listing hundreds of vanished temp roots buries the ones that matter.
        if plan.note == ABSENT_STATE:
            absent += 1
            continue
        lines.append(str(plan.installation.state))
        if plan.note:
            lines.append("  ({0})".format(plan.note))
            lines.append("")
            continue
        kinds: dict[str, tuple[int, int]] = {}
        for item in plan.reclaimable:
            count, size = kinds.get(item.kind, (0, 0))
            kinds[item.kind] = (count + 1, size + item.size)
        keeps: dict[str, int] = {}
        for item in plan.kept:
            keeps[item.kind] = keeps.get(item.kind, 0) + 1
        if kinds:
            lines.append("  {0:<14}{1:>7}{2:>10}".format("kind", "dirs", "bytes"))
            for kind in sorted(kinds):
                count, size = kinds[kind]
                lines.append(
                    "  {0:<14}{1:>7}{2:>10}".format(kind, count, human(size))
                )
        count = sum(item[0] for item in kinds.values())
        size = sum(item[1] for item in kinds.values())
        total_dirs += count
        total_bytes += size
        lines.append(
            "  {0:<14}{1:>7}{2:>10}   {3}".format("TOTAL", count, human(size), verb)
        )
        if keeps:
            lines.append(
                "  kept: {0}".format(
                    ", ".join(
                        "{0}x{1}".format(kind, keeps[kind]) for kind in sorted(keeps)
                    )
                )
            )
        if verbose:
            for item in plan.entries:
                lines.append(
                    "    {0:<8}{1:>9}  {2}  ({3})".format(
                        item.decision,
                        human(item.size) if item.size else "-",
                        item.path.name,
                        item.reason,
                    )
                )
        lines.append("")
    if absent:
        lines.append("{0} registry entries whose state root is gone".format(absent))
    lines.append(
        "{0}: {1} directories, {2}".format(verb, total_dirs, human(total_bytes))
    )
    if not applied and total_dirs:
        lines.append("dry run; nothing was removed. Pass --apply to reclaim.")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# entry point


def gc(
    installations: Iterable[Installation],
    *,
    keep: int = DEFAULT_KEEP,
    take_all: bool = False,
    min_age_s: float = DEFAULT_MIN_AGE_HOURS * 3600.0,
    apply: bool = False,
    now: float | None = None,
) -> tuple[Plan, ...]:
    plans: list[Plan] = []
    for installation in installations:
        if not installation.state.is_dir():
            plans.append(Plan(installation=installation, note=ABSENT_STATE))
            continue
        try:
            runs = read_runs(installation.database)
        except LedgerUnreadable as exc:
            plans.append(
                Plan(
                    installation=installation,
                    note="ledger unreadable ({0}); everything kept".format(exc),
                )
            )
            continue
        plan = classify(
            installation,
            runs,
            keep=keep,
            take_all=take_all,
            min_age_s=min_age_s,
            now=now,
        )
        if apply and plan.reclaimable:
            index = registration_index(installation, runs)
            for item in plan.reclaimable:
                remove_entry(
                    item, worktrees_root=installation.worktrees, index=index
                )
        plans.append(plan)
    return tuple(plans)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gc_worktrees.py",
        description="Reclaim dead scratch worktrees from a maestro installation.",
    )
    parser.add_argument(
        "--state",
        type=Path,
        default=None,
        help="one installation's runtime state root (default: every registry entry)",
    )
    parser.add_argument(
        "--registry",
        type=Path,
        default=DEFAULT_REGISTRY,
        help="registry to resolve installations from",
    )
    parser.add_argument(
        "--keep",
        type=int,
        default=DEFAULT_KEEP,
        help="keep the N most recent scratch trees per lane and kind",
    )
    parser.add_argument(
        "--all",
        dest="take_all",
        action="store_true",
        help="take the most-recent scratch trees too",
    )
    parser.add_argument(
        "--min-age-hours",
        type=float,
        default=DEFAULT_MIN_AGE_HOURS,
        help="never touch anything modified more recently than this",
    )
    parser.add_argument(
        "--apply", action="store_true", help="delete; without it nothing is removed"
    )
    parser.add_argument(
        "--verbose", action="store_true", help="print every path, not just totals"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.keep < 0:
        print("--keep must not be negative", file=sys.stderr)
        return 2
    if args.state is not None:
        installations: tuple[Installation, ...] = (
            installation_for_state(args.state),
        )
    else:
        installations = read_registry(args.registry)
        if not installations:
            print(
                "no installations in {0}; pass --state".format(args.registry),
                file=sys.stderr,
            )
            return 2
    plans = gc(
        installations,
        keep=args.keep,
        take_all=args.take_all,
        min_age_s=max(0.0, args.min_age_hours) * 3600.0,
        apply=args.apply,
    )
    print(render(plans, applied=args.apply, verbose=args.verbose))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
