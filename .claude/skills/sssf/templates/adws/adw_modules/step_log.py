"""Durable JSONL mirror of the operator step stream. Reporting, never state.

`FactoryConsole.step` already reports what a stage is doing while it runs, but
it reports it to a terminal: the line exists for as long as an operator is
looking at it and nowhere else. A dashboard watching a run from another process
had nothing to read, so a stage that takes minutes looked identical to a dead
scheduler from outside the CLI.

This module appends the same steps to `<runtime_state_root>/steps.jsonl`, one
compact JSON object per line, alongside the ledger. Three properties are the
whole point:

  * **Nothing reads this file back into a lifecycle decision.** Workflow
    authority is `lane_state.stage` and the immutable artifacts. A reader of
    this log may render it and may bound a run with the `run opened` /
    `run finished` lines; it may not transition anything on one, and no code in
    this runtime parses it. If a decision ever needs a fact that appears here,
    the fact belongs in the ledger.
  * **A failing write cannot fail a lane.** Every append swallows every
    exception. An unwritable path, a full disk, or a revoked directory is a
    lost report, never a failed run.
  * **A line is appended whole.** The file is opened `O_APPEND` and one
    complete UTF-8 buffer is written per call, so a reader tailing the file
    sees whole records and never half of one.

The file is created 0600 on first write, and is only ever appended to: it is
not truncated, rotated, or pruned by this runtime.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable

from .utils import now_iso

STEPS_FILENAME = "steps.jsonl"
STEPS_FILE_MODE = 0o600

#: The lane column for a line that belongs to the run rather than to a lane.
RUN_LANE_ID = "-"


class StepLog:
    """Append-only JSONL sink for one run's steps.

    Never raises. A step that cannot be written is a step nobody sees, which is
    strictly better than a run that dies because a report could not be filed.
    """

    def __init__(self, state_root: str | Path, run_id: str) -> None:
        self.run_id = run_id
        try:
            self.path: Path | None = Path(state_root) / STEPS_FILENAME
        except Exception:  # pragma: no cover - defensive, Path() on garbage
            self.path = None

    def append(self, lane_id: str, message: str, detail: str = "") -> None:
        """Append one record. Swallows everything it can go wrong on."""
        try:
            line = json.dumps(
                {
                    "ts": now_iso(),
                    "run_id": self.run_id,
                    "lane_id": str(lane_id),
                    "message": str(message),
                    "detail": str(detail or ""),
                },
                separators=(",", ":"),
                ensure_ascii=False,
            )
            self._write(line.encode("utf-8") + b"\n")
        except Exception:
            return

    def _write(self, payload: bytes) -> None:
        path = self.path
        if path is None:
            return
        existed = os.path.exists(path)
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        fd = os.open(path, flags, STEPS_FILE_MODE)
        try:
            if not existed:
                # O_CREAT's mode is masked by umask; the file is ours alone.
                os.fchmod(fd, STEPS_FILE_MODE)
            # One write per record: O_APPEND makes the offset update atomic, so
            # a tailing reader never observes a partial line. A short write is
            # not expected on a regular file; finishing it is still better than
            # dropping the tail of a record.
            written = os.write(fd, payload)
            while 0 < written < len(payload):
                payload = payload[written:]
                written = os.write(fd, payload)
        finally:
            os.close(fd)


class RunReporter:
    """The operator console and the durable step log, driven together.

    Presents exactly the surface `FactoryConsole` presents, so the scheduler and
    the actors stay ignorant of the filesystem: they are handed a callable that
    happens to do two things.
    """

    def __init__(
        self,
        run_id: str,
        state_root: str | Path,
        console: Any | None = None,
    ) -> None:
        from . import factory_console as fconsole

        self._console = console if console is not None else fconsole.FactoryConsole()
        self._log = StepLog(state_root, run_id)

    def opened(
        self,
        action: str,
        run_id: str,
        repository: str | Path,
        main_ref: str,
        lanes: Iterable[str],
    ) -> None:
        self._console.opened(action, run_id, repository, main_ref, lanes)
        self._log.append(RUN_LANE_ID, "run opened", str(action))

    def stage_started(self, lane_id: str, stage: Any) -> None:
        self._console.stage_started(lane_id, stage)

    def stage_completed(self, lane_id: str, previous: Any, current: Any) -> None:
        self._console.stage_completed(lane_id, previous, current)

    def step(self, lane_id: str, message: str, detail: str = "") -> None:
        try:
            self._console.step(lane_id, message, detail)
        finally:
            self._log.append(lane_id, message, detail)

    def finished(self, run_id: str, status: Any) -> None:
        self._console.finished(run_id, status)
        self._log.append(
            RUN_LANE_ID, "run finished", getattr(status, "value", str(status))
        )
