#!/usr/bin/env python3
"""Print every gate on a lane's path, read from the ledger and from git.

This tool is strictly read-only. It opens `lifecycle.sqlite3` with SQLite's
`mode=ro` URI, it never invokes `maestro.py`, it never starts, resumes, or
amends a run, and it never writes to the ledger, the vault, or any worktree.
Its only side effect is what it prints.

It exists because a `run start` / `run resume` line handed to an operator
without the gate values behind it is a guess. Every value below is read; none
is inferred, summarized, or interpreted.

Exit status is 1 when any lane is WAITING_FOR_USER or when the template
runtime and the run's bound repository runtime are not level.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping, Optional, Sequence

_TOOLS_DIR = Path(__file__).resolve().parent
_RUNTIME_ROOT = _TOOLS_DIR.parent
if str(_RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(_RUNTIME_ROOT))

from adw_modules import scheduler_types as st  # noqa: E402
from adw_modules.code_review import _COLLECTION_REVISE, _RUNNER_REVISE  # noqa: E402
from adw_modules.reporting_registry import registry_path  # noqa: E402
from adw_modules.scheduler import _sealed_error_history, _stalled  # noqa: E402

sys.path.insert(0, str(_TOOLS_DIR))
import runtime_sync  # noqa: E402

#: The template layout this tool compares a deployment against.
TEMPLATE_RELATIVE = Path(".claude") / "skills" / "sssf" / "templates" / "adws"

#: `observed_behavior` of the two findings the harness substitutes when a
#: reviewer voted PASS over a red suite. Their presence is the record that the
#: reviewer's own verdict was not the one written down.
_COERCED_TO_REVISE = (
    _RUNNER_REVISE["observed_behavior"],
    _COLLECTION_REVISE["observed_behavior"],
)

_ABSENT = "-"


class Unresolved(RuntimeError):
    """The run could not be located in any registered installation."""


# --------------------------------------------------------------------------
# resolution
# --------------------------------------------------------------------------


def read_registry(path: Optional[Path] = None) -> list[Mapping[str, Any]]:
    """Installations recorded by `reporting_registry`, newest first."""
    target = Path(path) if path is not None else registry_path()
    if not target.is_file():
        return []
    parsed = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        return []
    installations = parsed.get("installations")
    if not isinstance(installations, list):
        return []
    return [item for item in installations if isinstance(item, Mapping)]


def open_readonly(database: str | Path) -> sqlite3.Connection:
    """A connection that SQLite itself refuses to write through."""
    resolved = Path(database)
    conn = sqlite3.connect(
        "file:{0}?mode=ro".format(resolved.as_posix()), uri=True
    )
    conn.row_factory = sqlite3.Row
    return conn


def resolve_run(
    run_id: str, registry: Optional[Path] = None
) -> tuple[sqlite3.Connection, sqlite3.Row, str]:
    """Find the installation holding `run_id` and return its ledger row.

    Resolution is by the same registry the scheduler writes, honouring the
    same `MAESTRO_REGISTRY` override. The row is authority for the run's
    `runtime_state_root` and bound repository; the registry only says which
    database to look in.
    """
    for entry in read_registry(registry):
        database = entry.get("database")
        if not isinstance(database, str) or not database:
            continue
        if not Path(database).is_file():
            continue
        try:
            conn = open_readonly(database)
        except sqlite3.Error:
            continue
        try:
            row = conn.execute(
                "SELECT * FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
        except sqlite3.Error:
            conn.close()
            continue
        if row is None:
            conn.close()
            continue
        return conn, row, database
    raise Unresolved(run_id)


# --------------------------------------------------------------------------
# ledger reads
# --------------------------------------------------------------------------


def _loads(raw: Any) -> Mapping[str, Any]:
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, Mapping) else {}


def lane_rows(conn: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT lane_id, stage, updated_at FROM lane_state "
            "WHERE run_id=? ORDER BY lane_id",
            (run_id,),
        )
    )


def latest_artifact(
    conn: sqlite3.Connection, run_id: str, lane_id: str, kind: str
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM lane_artifacts WHERE run_id=? AND lane_id=? "
        "AND artifact_kind=? ORDER BY sequence DESC LIMIT 1",
        (run_id, lane_id, kind),
    ).fetchone()


def review_rounds(
    conn: sqlite3.Connection, run_id: str, lane_id: str, limit: int = 5
) -> list[sqlite3.Row]:
    """The last `limit` CODE_REVIEW records, oldest first."""
    rows = list(
        conn.execute(
            "SELECT * FROM lane_artifacts WHERE run_id=? AND lane_id=? "
            "AND artifact_kind=? ORDER BY sequence DESC LIMIT ?",
            (run_id, lane_id, st.ArtifactKind.CODE_REVIEW.value, limit),
        )
    )
    return list(reversed(rows))


def reviewer_verdict(payload: Mapping[str, Any]) -> str:
    """The verdict the reviewer voted, before the harness coerced it.

    Read off the record, not guessed. A non-empty `advisory_findings` is what
    a REVISE demoted to PASS over a green suite leaves behind; a substituted
    runner finding is what a PASS promoted to REVISE over a red one leaves
    behind. Anything else, the reviewer's verdict is the recorded one.
    """
    recorded = str(payload.get("verdict") or _ABSENT)
    if payload.get("advisory_findings"):
        return st.ReviewerVerdict.REVISE.value
    for finding in payload.get("findings") or ():
        if not isinstance(finding, Mapping):
            continue
        if finding.get("observed_behavior") in _COERCED_TO_REVISE:
            return st.ReviewerVerdict.PASS.value
    return recorded


def builder_checkout(runtime_state_root: str, run_id: str, lane_id: str) -> Path:
    return Path(runtime_state_root) / "worktrees" / run_id / lane_id / "builder" / "checkout"


def git_head(checkout: Path) -> str:
    """`HEAD` of a checkout, or `-` when there is no readable checkout.

    `rev-parse` is the whole vocabulary here. No command this tool runs can
    mutate a worktree.
    """
    if not (checkout / ".git").exists():
        return _ABSENT
    result = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return _ABSENT
    return (result.stdout or "").strip() or _ABSENT


# --------------------------------------------------------------------------
# runtime parity
# --------------------------------------------------------------------------


def template_root(start: Optional[Path] = None) -> Optional[Path]:
    """The `.claude/skills/sssf/templates/adws` this tool compares against."""
    here = Path(start) if start is not None else _RUNTIME_ROOT
    if runtime_sync.classify(here) == runtime_sync.TEMPLATE:
        return here
    for parent in [here, *here.parents]:
        candidate = parent / TEMPLATE_RELATIVE
        if candidate.is_dir():
            return candidate
    return None


def runtime_sync_line(repository: str) -> tuple[str, bool]:
    """One line: LEVEL, or the names of the files that differ."""
    source = template_root()
    if source is None:
        return "NO_TEMPLATE", False
    destination = Path(repository) / "adws"
    if not destination.is_dir():
        return "NO_DESTINATION {0}".format(destination), False
    report = runtime_sync.compare(
        runtime_sync.describe_copy(source),
        runtime_sync.describe_copy(destination),
    )
    if report.is_level:
        return "LEVEL compared={0}".format(report.compared), True
    names = sorted(
        {item.relative_path for item in report.differing} | set(report.missing_files)
    )
    return " ".join(names), False


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------


def _summary_field(summary: Mapping[str, Any], key: str) -> str:
    value = summary.get(key)
    return _ABSENT if value is None else str(value)


def lane_table(
    conn: sqlite3.Connection,
    run_id: str,
    lane_row: sqlite3.Row,
    runtime_state_root: str,
    sync_line: str,
) -> list[tuple[str, str]]:
    lane_id = str(lane_row["lane_id"])
    rows: list[tuple[str, str]] = [
        ("stage", str(lane_row["stage"])),
        ("stage_updated_at", str(lane_row["updated_at"])),
    ]

    wait = latest_artifact(conn, run_id, lane_id, st.ArtifactKind.USER_WAIT.value)
    if wait is None:
        rows.append(("user_wait", _ABSENT))
    else:
        payload = _loads(wait["payload_json"])
        rows.append(
            (
                "user_wait",
                "seq={0} reason={1} resume_stage={2} created_at={3} "
                "artifact_id={4}".format(
                    wait["sequence"],
                    payload.get("wait_reason", _ABSENT),
                    payload.get("resume_stage", _ABSENT),
                    wait["created_at"],
                    wait["artifact_id"],
                ),
            )
        )

    rounds = review_rounds(conn, run_id, lane_id)
    if not rounds:
        rows.append(("review", _ABSENT))
    for index, record in enumerate(rounds):
        payload = _loads(record["payload_json"])
        summary = payload.get("public_result_summary") or {}
        if not isinstance(summary, Mapping):
            summary = {}
        recorded = str(payload.get("verdict") or _ABSENT)
        voted = reviewer_verdict(payload)
        rows.append(
            (
                "review[{0}]".format(index - len(rounds)),
                "seq={0} executed={1} passed={2} failed={3} errored={4} "
                "reviewer={5} recorded={6} agree={7}".format(
                    record["sequence"],
                    _summary_field(summary, "executed"),
                    _summary_field(summary, "passed"),
                    _summary_field(summary, "failed"),
                    _summary_field(summary, "errored"),
                    voted,
                    recorded,
                    voted == recorded,
                ),
            )
        )

    history = _sealed_error_history(SimpleNamespace(conn=conn), run_id, lane_id)
    rows.append(("sealed_error_history", json.dumps(history)))
    rows.append(("stalled", str(_stalled(history))))

    builder = latest_artifact(
        conn, run_id, lane_id, st.ArtifactKind.BUILDER_OUTPUT.value
    )
    candidate = _ABSENT
    if builder is not None:
        candidate = str(_loads(builder["payload_json"]).get("candidate_sha") or _ABSENT)
    checkout = builder_checkout(runtime_state_root, run_id, lane_id)
    head = git_head(checkout)
    rows.append(("candidate_sha", candidate))
    rows.append(("builder_checkout", str(checkout)))
    rows.append(("builder_head", head))
    rows.append(
        (
            "candidate_matches_head",
            str(candidate != _ABSENT and head != _ABSENT and candidate == head),
        )
    )
    rows.append(("runtime_sync", sync_line))
    return rows


def render(run_id: str, lane_id: str, rows: Sequence[tuple[str, str]]) -> str:
    width = max([len(name) for name, _ in rows] + [len("FIELD")])
    lines = ["LANE_GATES run={0} lane={1}".format(run_id, lane_id)]
    lines.append("{0}  {1}".format("FIELD".ljust(width), "VALUE"))
    for name, value in rows:
        lines.append("{0}  {1}".format(name.ljust(width), value))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lane_gates.py",
        description="Read-only gate table for one run's lanes.",
    )
    parser.add_argument("--run", required=True, help="run id")
    parser.add_argument("--lane", default=None, help="one lane id")
    parser.add_argument(
        "--registry",
        default=None,
        help="registry path (defaults to the scheduler's)",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    registry = Path(args.registry) if args.registry else None
    try:
        conn, run_row, _database = resolve_run(args.run, registry)
    except Unresolved:
        sys.stdout.write("LANE_GATES run={0} UNRESOLVED\n".format(args.run))
        return 1
    try:
        runtime_state_root = str(run_row["runtime_state_root"])
        repository = str(run_row["target_repository_root"])
        sync_line, level = runtime_sync_line(repository)
        lanes = lane_rows(conn, args.run)
        if args.lane is not None:
            lanes = [row for row in lanes if str(row["lane_id"]) == args.lane]
        waiting = False
        blocks: list[str] = []
        for lane_row in lanes:
            if str(lane_row["stage"]) == st.LaneStage.WAITING_FOR_USER.value:
                waiting = True
            blocks.append(
                render(
                    args.run,
                    str(lane_row["lane_id"]),
                    lane_table(
                        conn, args.run, lane_row, runtime_state_root, sync_line
                    ),
                )
            )
        if not blocks:
            blocks.append(
                "LANE_GATES run={0} lane={1} NO_LANE".format(
                    args.run, args.lane if args.lane else _ABSENT
                )
            )
        sys.stdout.write("\n\n".join(blocks) + "\n")
        return 0 if (level and not waiting) else 1
    finally:
        conn.close()


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
