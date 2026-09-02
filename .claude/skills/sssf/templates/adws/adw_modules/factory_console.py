"""Rich, append-only operator output for artifact-factory runs."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.text import Text

from . import scheduler_types as st


class FactoryConsole:
    """Render durable run and lane progress without becoming workflow state."""

    def __init__(self, console: Console | None = None) -> None:
        self._console = console or Console(highlight=False, soft_wrap=True)

    def opened(
        self,
        action: str,
        run_id: str,
        repository: str | Path,
        main_ref: str,
        lanes: Iterable[str],
    ) -> None:
        lane_list = ", ".join(lanes) or "none"
        body = Text.from_markup(
            "\n".join(
                (
                    f"[dim]action[/dim]      {escape(action)}",
                    f"[dim]run[/dim]         [bold]{escape(run_id)}[/bold]",
                    f"[dim]repository[/dim]  {escape(str(repository))}",
                    f"[dim]main ref[/dim]    {escape(main_ref)}",
                    f"[dim]lanes[/dim]       {escape(lane_list)}",
                )
            )
        )
        self._console.print(
            Panel(body, title="[bold cyan]Maestro factory[/bold cyan]", expand=False)
        )

    def stage_started(self, lane_id: str, stage: st.LaneStage) -> None:
        self._console.print(
            f"[cyan]▶[/cyan] [bold]{escape(lane_id)}[/bold]  {escape(stage.value)}"
        )

    def step(self, lane_id: str, message: str, detail: str = "") -> None:
        """One step inside a stage, printed as it happens.

        A stage is not an atomic act. REVIEWING_CODE provisions a tree, runs a
        sealed suite, dispatches a reviewer, waits on its envelope, and may ask
        it a second time -- minutes apart, and every one of them was silent
        between the stage's start line and its completion line. An operator
        watching that could not tell provisioning from a hung agent from a dead
        scheduler, which is the report this method exists to answer.
        """
        line = f"[dim]·[/dim] [bold]{escape(lane_id)}[/bold]  {escape(message)}"
        if detail:
            line += f"  [dim]{escape(detail)}[/dim]"
        self._console.print(line)

    def stage_completed(
        self,
        lane_id: str,
        previous: st.LaneStage,
        current: st.LaneStage,
    ) -> None:
        self._console.print(
            f"[green]✓[/green] [bold]{escape(lane_id)}[/bold]  "
            f"{escape(previous.value)} [dim]→[/dim] {escape(current.value)}"
        )

    def finished(self, run_id: str, status: st.RunStatus) -> None:
        color = "green" if status is st.RunStatus.COMPLETE else "yellow"
        self._console.print(
            f"[{color}]■[/{color}] [bold]{escape(run_id)}[/bold]  "
            f"{escape(status.value)}"
        )
