"""Transport prompt-size arithmetic. Not a retry or grant budget."""

from __future__ import annotations

from typing import Tuple

#: Bytes per token. Under-estimate so an oversized prompt is refused.
BYTES_PER_TOKEN = 3.0

#: Share of the published context window a prompt may occupy.
HANDOFF_CONTEXT_FRACTION = 0.5

#: Routes that publish a measurable context window.
ROUTES_PUBLISHING_A_WINDOW: Tuple[str, ...] = ("omp",)


def route_publishes_a_window(route: str) -> bool:
    """Whether `route` has a catalog a prompt can be size-checked against."""
    return route in ROUTES_PUBLISHING_A_WINDOW


def estimate_tokens(text: str) -> int:
    return estimate_tokens_for_bytes(len(text))


def estimate_tokens_for_bytes(size: int) -> int:
    """Token estimate from a byte count. Uses `stat()` size, not file contents."""
    return int(max(0, size) / BYTES_PER_TOKEN) + 1


def handoff_budget(
    context_window_tokens: int, fraction: float = HANDOFF_CONTEXT_FRACTION
) -> int:
    """Maximum prompt tokens admitted for a published window. Transport only."""
    return int(context_window_tokens * fraction)
