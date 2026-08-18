"""B13's arithmetic and its one fact about routes, as a leaf module.

MAESTRO_architecture.md §3.6 B13 requires every handoff to be size-checked
against the target's context window *before* dispatch, failing closed. That
rule was written in `code_review`, next to the reviewer handoff it was found
on, and applied at three CLI dispatch sites. The site it could not reach is the
one that matters: `launcher.HerdrLauncher.launch`, which every prompt this
system dispatches passes through. `launcher` cannot import `code_review` —
`code_review` imports `scheduler_types`, which imports `worktree`, which
imports `launcher` — so the rule lived above the only place it could not be
bypassed.

So the arithmetic moves down here, below both. This module imports nothing from
the package and must keep it that way: it is the floor the check stands on.

`code_review` re-exports every name below, so the rule still reads as one rule
from the reviewer's side; there is exactly one definition of it.
"""

from __future__ import annotations

from typing import Optional, Tuple

#: Bytes per token. A deliberate under-estimate of tokenizer efficiency on
#: source diffs, which tokenize worse than prose: punctuation, identifiers, and
#: indentation all fragment. Being wrong in this direction refuses a handoff
#: that would have fit; being wrong in the other direction is B13.
BYTES_PER_TOKEN = 3.0

#: The share of the window the input may occupy. Half, because the target must
#: also *think* and answer inside the same window, and because occupancy above
#: 0.8 is refused after the fact by `finalization.check_occupancy` — a handoff
#: admitted at 0.9 would be a turn spent to reach a guaranteed rejection.
HANDOFF_CONTEXT_FRACTION = 0.5

#: The routes that publish a context window the harness can measure against.
#:
#: B13 says fail closed, and this is what "closed" can mean without lying: on a
#: route with a model catalog, a model the catalog does not carry is an
#: unmeasured window and refuses. A route that publishes no catalog at all
#: publishes nothing to be closed against — refusing every dispatch on it would
#: not be a size check, it would be a route that no longer launches.
#:
#: This is the guard's *input*, narrowed, and deliberately not an allowlist of
#: call sites: it names a property of the route (does a catalog exist?), so a
#: new dispatch site cannot exempt itself, and a route that starts publishing a
#: catalog is covered by editing one tuple. `_deliver_author_turn`'s `claude`
#: route is uncovered because of what the route is, stated here once, rather
#: than because some call site was left out.
ROUTES_PUBLISHING_A_WINDOW: Tuple[str, ...] = ("omp",)


def route_publishes_a_window(route: str) -> bool:
    """Whether `route` has a catalog a prompt can be size-checked against."""
    return route in ROUTES_PUBLISHING_A_WINDOW


def estimate_tokens(text: str) -> int:
    return estimate_tokens_for_bytes(len(text))


def estimate_tokens_for_bytes(size: int) -> int:
    """The same estimate, over a byte count rather than a string.

    The chokepoint check measures a file with `stat()` instead of reading it:
    the number it needs is a size, reading the prompt into memory to get one
    would be the only place in the dispatch path that opens it, and §1.2 is
    satisfied more obviously by arithmetic on a size than by arithmetic on
    bytes nobody looked at.
    """
    return int(max(0, size) / BYTES_PER_TOKEN) + 1


def handoff_budget(context_window_tokens: int,
                   fraction: float = HANDOFF_CONTEXT_FRACTION) -> int:
    """How many input tokens a window of this size may be handed."""
    return int(context_window_tokens * fraction)
