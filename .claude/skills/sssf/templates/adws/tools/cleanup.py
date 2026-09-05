#!/usr/bin/env python3
"""Close the Herdr lane panes a finished run left open in this repository.

COMPLETE cleanup closes a run's lane panes as its last act, and it is the only
thing that does. It is also the one part of a run that can refuse *after* the
publication has landed -- `CLEANUP_REFUSED` -- and when it refuses it stops at
the first pane, so the rest stay open. Run 98fa094e published its integration
ref and left twenty panes behind that way. Nothing else reclaims them, so the
next run splits its lanes beside the last run's corpses.

This tool is that reclaimer. It is transport only: it closes panes and nothing
else. It never reads or writes the ledger's lane state, never touches
artifacts, the vault, receipts, locks, worktrees or the target repository, and
it cannot advance, block, or fail a lane. A pane is not workflow authority, so
closing one decides nothing.

    cleanup.py                     # from inside the repo, dry run
    cleanup.py --apply             # actually close them
    cleanup.py --run <run_id>      # only that run's panes
    cleanup.py --repo PATH         # a repository other than the cwd's

Dry run is the default. ``--apply`` is the only thing that closes anything.

What it will close, and nothing else: a pane Herdr reports with Maestro's own
``kind=lane`` token whose ``run_id`` token names a run in *this* repository's
ledger. That selector is what keeps it away from an operator's own panes, from
another repository's run, and from the parent Space -- which Maestro never
closes, because it is the operator's. Closing a lane's last pane closes that
lane's linked child workspace, which is Herdr's behaviour and the point.

**A live run's panes are its agents.** Closing one kills the turn it is in the
middle of, so this refuses while a `maestro.py run` process is alive for the
repository, and names the pid. `--force` overrides that and is the operator
saying they know the process is dead; it is never the answer to "the refusal is
inconvenient".
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Mapping, Sequence

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

from adw_modules import launcher as lch  # noqa: E402

DEFAULT_REGISTRY = Path.home() / ".maestro" / "registry.json"


class CleanupRefused(RuntimeError):
    """Named so the caller sees a reason rather than a traceback."""


def _repository_root(start: Path) -> Path:
    """The primary working tree of the repository `start` sits in."""
    try:
        return lch.git_primary_workdir(start)
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed
        raise CleanupRefused(
            "NOT_A_GIT_REPOSITORY:{0}: {1}".format(start, exc)
        ) from exc


def _installation(registry: Path, repository: Path) -> Mapping[str, str]:
    try:
        payload = json.loads(registry.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CleanupRefused("REGISTRY_ABSENT:{0}".format(registry)) from exc
    except (OSError, ValueError) as exc:
        raise CleanupRefused(
            "REGISTRY_UNREADABLE:{0}: {1}".format(registry, exc)
        ) from exc
    for entry in payload.get("installations") or ():
        if not isinstance(entry, dict):
            continue
        recorded = str(entry.get("repository") or "")
        if not recorded:
            continue
        try:
            same = Path(recorded).resolve() == repository
        except OSError:
            continue
        if same:
            return {str(k): str(v) for k, v in entry.items()}
    raise CleanupRefused("NO_INSTALLATION_FOR:{0}".format(repository))


def _run_ids(database: Path) -> Dict[str, str]:
    """Every run this installation's ledger holds, and when it last moved."""
    if not database.exists():
        raise CleanupRefused("LEDGER_ABSENT:{0}".format(database))
    uri = "file:{0}?mode=ro".format(database)
    try:
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise CleanupRefused(
            "LEDGER_UNREADABLE:{0}: {1}".format(database, exc)
        ) from exc
    try:
        rows = connection.execute(
            "select run_id, updated_at from runs"
        ).fetchall()
    except sqlite3.Error as exc:
        raise CleanupRefused(
            "LEDGER_UNREADABLE:{0}: {1}".format(database, exc)
        ) from exc
    finally:
        connection.close()
    return {str(run_id): str(updated or "") for run_id, updated in rows}


def _live_run_pids(repository: Path) -> List[str]:
    """`maestro.py run ...` processes whose command line names `repository`.

    Direct evidence, and the only kind available: `locks/run.lock` is taken and
    released around each lane mutation rather than held for the run, so probing
    it answers "is a lane mutating right now", which is not the question.
    """
    try:
        listing = subprocess.run(
            ["ps", "-Ao", "pid=,command="],
            capture_output=True,
            text=True,
            check=False,
            timeout=20.0,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    mine = str(os.getpid())
    found: List[str] = []
    for line in (listing.stdout or "").splitlines():
        text = line.strip()
        if not text:
            continue
        pid, _, command = text.partition(" ")
        if pid == mine or "maestro.py" not in command:
            continue
        if " run " not in command:
            continue
        if str(repository) not in command:
            continue
        found.append(pid)
    return found


def _herdr_path() -> str:
    found = shutil.which("herdr")
    if not found:
        raise CleanupRefused("HERDR_UNAVAILABLE:herdr not on PATH")
    return found


def _herdr(herdr: str, *args: str) -> dict:
    try:
        result = subprocess.run(
            [herdr, *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=60.0,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CleanupRefused(
            "HERDR_UNAVAILABLE:{0}: {1}".format(" ".join(args), exc)
        ) from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise CleanupRefused(
            "HERDR_REFUSED:{0}: {1}".format(" ".join(args), detail[-400:])
        )
    try:
        payload = json.loads((result.stdout or "").strip() or "{}")
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _lane_panes(
    herdr: str, runs: Mapping[str, str], only_run: str
) -> List[Mapping[str, str]]:
    payload = _herdr(herdr, "pane", "list")
    result = payload.get("result", payload)
    items = result.get("panes") if isinstance(result, dict) else None
    selected: List[Mapping[str, str]] = []
    for item in items or ():
        if not isinstance(item, dict):
            continue
        tokens = item.get("tokens")
        if not isinstance(tokens, dict):
            continue
        if str(tokens.get(lch.METADATA_TOKEN_KIND) or "") != lch.METADATA_KIND_LANE:
            continue
        run_id = str(tokens.get(lch.METADATA_TOKEN_RUN) or "")
        if run_id not in runs:
            continue
        if only_run and run_id != only_run:
            continue
        selected.append(
            {
                "pane_id": str(item.get("pane_id") or ""),
                "workspace_id": str(item.get("workspace_id") or ""),
                "run_id": run_id,
                "lane": str(tokens.get(lch.METADATA_TOKEN_LANE) or ""),
                "role": str(tokens.get(lch.METADATA_TOKEN_ROLE) or ""),
                "updated_at": runs.get(run_id, ""),
            }
        )
    selected.sort(key=lambda row: (row["run_id"], row["lane"], row["role"]))
    return [row for row in selected if row["pane_id"]]


def _close(herdr: str, panes: Sequence[Mapping[str, str]]) -> tuple[int, int]:
    closed = 0
    refused = 0
    for pane in panes:
        try:
            # Positional. `--pane` is a different verb's flag and fails here.
            _herdr(herdr, "pane", "close", pane["pane_id"])
        except CleanupRefused as exc:
            refused += 1
            print("  REFUSED {0}: {1}".format(pane["pane_id"], exc))
            continue
        closed += 1
        print("  closed  {0}".format(pane["pane_id"]))
    return closed, refused


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cleanup.py", description=__doc__ or "", add_help=True
    )
    parser.add_argument(
        "--repo",
        default=".",
        help="a path inside the repository whose lane panes to close "
        "(default: the working directory)",
    )
    parser.add_argument(
        "--registry",
        default=str(DEFAULT_REGISTRY),
        help="registry to resolve the installation from",
    )
    parser.add_argument(
        "--run", default="", help="only this run's panes, instead of every run's"
    )
    parser.add_argument(
        "--apply", action="store_true", help="close them; without it nothing closes"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="close even while a maestro run process is alive for this "
        "repository -- its agents are in those panes",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        repository = _repository_root(Path(args.repo).resolve())
        installation = _installation(Path(args.registry).expanduser(), repository)
        runs = _run_ids(Path(installation["database"]))
        herdr = _herdr_path()
        panes = _lane_panes(herdr, runs, str(args.run or ""))
    except CleanupRefused as exc:
        print(json.dumps({"detail": str(exc), "outcome": "CLEANUP_REFUSED"}))
        return 3

    print("repository {0}".format(repository))
    print("state      {0}".format(installation.get("state", "")))
    if not panes:
        print("no lane panes open for this repository")
        return 0

    for pane in panes:
        print(
            "  {pane_id:<10} {run_id:.8}  {lane}/{role}".format(**pane)
        )
    print("{0} lane pane(s)".format(len(panes)))

    live = _live_run_pids(repository)
    if live and not args.force:
        print(
            json.dumps(
                {
                    "detail": "RUN_PROCESS_ALIVE:{0}".format(",".join(live)),
                    "outcome": "CLEANUP_REFUSED",
                }
            )
        )
        return 3
    if live:
        print("forcing past live run process(es) {0}".format(",".join(live)))

    if not args.apply:
        print("dry run; pass --apply to close them")
        return 0

    closed, refused = _close(herdr, panes)
    print("closed {0}, refused {1}".format(closed, refused))
    return 3 if refused else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
