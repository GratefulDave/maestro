"""Vendor, model, and route recorded on an attempt at launch.

§1.2: these are the launcher's own configured facts, written at the moment it
dispatches. They are never scraped from pane text, prompt text, a free-text
envelope field, or an agent's claim about which model it is.

A pre-existing attempt has no such keys. That absence is "not recorded",
never an empty string and never a default vendor — the same NULL-vs-empty
discipline the rest of the ledger keeps.

§3.6 B15: `identity_from_record` and `display` are the readers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional

from . import scheduler_types as st

#: extra_json keys. One definition each; call sites import these.
VENDOR_KEY = "vendor"
MODEL_KEY = "model"
ROUTE_KEY = "route"

#: Human render for a field that was never written. Distinct from any
#: recorded value, including the empty string (which is also not recorded).
NOT_RECORDED = "not recorded"


@dataclass(frozen=True)
class AttemptIdentity:
    """Vendor, model, and route as stored on one attempt, or not recorded."""

    vendor: Optional[str]
    model: Optional[str]
    route: Optional[str]


def recorded_value(value: object) -> Optional[str]:
    """A written identity string, or None when the field was not recorded.

    Missing, non-string, blank, and whitespace-only values are all absence.
    """
    if isinstance(value, str):
        text = value.strip()
        if text:
            return text
    return None


def launch_identity_extra(
        *, vendor: object = None, model: object = None,
        route: object = None) -> Dict[str, str]:
    """The identity keys the launcher can actually see at dispatch.

    Omits any field that is not recorded. Guessing a default is forbidden.
    """
    extra: Dict[str, str] = {}
    for key, value in (
            (VENDOR_KEY, vendor), (MODEL_KEY, model), (ROUTE_KEY, route)):
        recorded = recorded_value(value)
        if recorded is not None:
            extra[key] = recorded
    return extra


def identity_from_record(record: st.AttemptRecord) -> AttemptIdentity:
    """Typed accessor over `AttemptRecord.extra`. None means not recorded."""
    extra: Mapping[str, Any] = record.extra or {}
    return AttemptIdentity(
        vendor=recorded_value(extra.get(VENDOR_KEY)),
        model=recorded_value(extra.get(MODEL_KEY)),
        route=recorded_value(extra.get(ROUTE_KEY)))


def display(value: Optional[str]) -> str:
    """Operator render: a recorded value, or the distinct not-recorded token."""
    return value if value is not None else NOT_RECORDED
