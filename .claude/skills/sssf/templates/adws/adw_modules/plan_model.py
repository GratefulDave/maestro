"""Compiler-only plan parse/refusal types. Shared DTOs live in scheduler_types."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import PurePath, PurePosixPath
from typing import Any, Mapping, Optional, Tuple

SCHEMA_VERSION = "maestro-plan.artifact-factory.v1"
NO_PLAN_ARTIFACT_REF = "NO_PLAN_ARTIFACT_REF"

PLAN_KEYS = frozenset({"schema_version", "lanes"})
LANE_KEYS = frozenset({"id", "needs", "outputs", "spec", "acceptance", "lane_kind"})
ACCEPTANCE_KEYS = frozenset({"criterion", "gating", "observation_seam"})


@dataclass(frozen=True)
class AcceptanceCriterion:
    """One public acceptance criterion, with its declared observation seam.

    A criterion authored as a plain string is advisory: nothing gates on it and
    no seam is required. A criterion authored as an object may declare
    ``gating: true``, which makes it an obligation a tests lane must discharge,
    and a gating obligation must name the ``observation_seam`` a case can assert
    on from the public contract. Gating is declared, never read out of the prose.
    """

    criterion: str
    gating: bool = False
    observation_seam: Optional[str] = None

    @property
    def public_text(self) -> str:
        """What reaches the tester, builder, and reviewer as acceptance."""
        if not self.observation_seam:
            return self.criterion
        return "{0} [observable: {1}]".format(self.criterion, self.observation_seam)

    def canonical(self) -> Any:
        """The authored form, normalized: a plain string stays a plain string."""
        if not self.gating and self.observation_seam is None:
            return self.criterion
        payload: dict = {"criterion": self.criterion, "gating": self.gating}
        if self.observation_seam is not None:
            payload["observation_seam"] = self.observation_seam
        return payload


def parse_acceptance_item(raw: Any) -> Optional[AcceptanceCriterion]:
    """One acceptance criterion, or None if the authored item is inadmissible.

    Accepts the historical plain-string form and the object form carrying the
    declared ``gating`` flag and ``observation_seam``. No prose is inspected.
    """
    if isinstance(raw, str):
        return AcceptanceCriterion(raw) if raw.strip() else None
    if not isinstance(raw, dict):
        return None
    if set(raw) - ACCEPTANCE_KEYS:
        return None
    criterion = raw.get("criterion")
    if not isinstance(criterion, str) or not criterion.strip():
        return None
    gating = raw.get("gating", False)
    if not isinstance(gating, bool):
        return None
    seam = raw.get("observation_seam")
    if seam is not None and (not isinstance(seam, str) or not seam.strip()):
        return None
    return AcceptanceCriterion(criterion, gating, seam)


class PlanParseError(ValueError):
    """Stored bytes are not a UTF-8 JSON object."""


@dataclass(frozen=True)
class PlanRefusal:
    """One objective compiler refusal. Not a workflow stage."""

    code: str
    pointer: str
    message: str


class PlanCompileError(ValueError):
    """The authored plan failed an objective check."""

    def __init__(self, refusals: Tuple[PlanRefusal, ...]) -> None:
        if not refusals:
            raise ValueError("PlanCompileError requires at least one refusal")
        self.refusals = refusals
        super().__init__(
            "; ".join(
                "{0} {1}: {2}".format(item.code, item.pointer, item.message)
                for item in refusals
            )
        )


def parse_stored_mapping(stored: bytes) -> Mapping[str, Any]:
    if not isinstance(stored, (bytes, bytearray)):
        raise PlanParseError("plan bytes are required")
    try:
        data = json.loads(bytes(stored).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PlanParseError("not UTF-8 JSON") from exc
    if not isinstance(data, dict):
        raise PlanParseError("plan root must be a JSON object")
    return data


def normalize_declared_output(raw: Any) -> Optional[str]:
    """Exact repository-relative POSIX file path, or None if inadmissible.

    Directories, globs, absolute paths, empty/`.`/`..` components, doubled
    separators, and non-operator spellings are refused rather than rewritten.
    Comparison is byte-exact on the returned string. No filesystem is consulted.
    """
    if not isinstance(raw, str) or not raw:
        return None
    if any(ord(char) < 32 or ord(char) == 127 for char in raw):
        return None
    if "\\" in raw or "//" in raw:
        return None
    if raw.endswith("/"):
        return None
    # `*` and `?` only. A bracket is not a glob here, and refusing it made
    # whole framework conventions unownable: Astro, Next.js, SvelteKit, Remix
    # and Nuxt all spell a dynamic route `[slug].astro` / `[id].tsx`, so no
    # lane could declare a single dynamic page. FDAdb's paid-panel regression
    # is exactly a change to a helper whose ten callers are all `[slug].astro`
    # -- the repair plan named them, as the authoring rule now requires, and
    # the compiler refused all ten.
    #
    # Nothing globs a declared output, which is what makes this safe rather
    # than lenient: `validate_declared_ownership` tests set membership on the
    # exact strings (`git_publication`), `permissions._matches` compares byte
    # equality unless the pattern itself carries `*` or `?` and `re.escape`s a
    # bracket besides, `outputs_conflict` below is prefix arithmetic, and no
    # `glob`/`fnmatch` in the runtime is ever handed one. Re-check that before
    # widening this further.
    if any(char in raw for char in "*?"):
        return None
    candidate = PurePosixPath(raw)
    native = PurePath(raw)
    if candidate.is_absolute() or native.is_absolute() or candidate.anchor:
        return None
    parts = candidate.parts
    if not parts or any(part in ("", ".", "..") for part in parts):
        return None
    if candidate.as_posix() != raw:
        return None
    return raw


def outputs_conflict(left: str, right: str) -> bool:
    if left == right:
        return True
    return left.startswith(right + "/") or right.startswith(left + "/")
