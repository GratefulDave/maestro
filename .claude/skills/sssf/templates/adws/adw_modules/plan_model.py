"""Compiler-only plan parse/refusal types. Shared DTOs live in scheduler_types."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import PurePath, PurePosixPath
from typing import Any, Mapping, Optional, Tuple

SCHEMA_VERSION = "maestro-plan.artifact-factory.v1"
NO_PLAN_ARTIFACT_REF = "NO_PLAN_ARTIFACT_REF"

# `approval` is the projection's signed binding to a plan-contract receipt
# (`plan_approval`). The compiler does not judge it and it is not part of the
# canonical document or digest; `run start` verifies it.
PLAN_KEYS = frozenset({"schema_version", "lanes", "approval"})
LANE_KEYS = frozenset({"id", "needs", "outputs", "spec", "acceptance", "lane_kind"})
ACCEPTANCE_KEYS = frozenset(
    {"criterion", "gating", "observation_seam", "decided_by", "restriction"}
)
RESTRICTION_KEYS = frozenset(
    {"polarity", "has_exception_ids", "has_preconditions", "external_store"}
)
DECIDED_BY_EXAMPLE_KEYS = frozenset({"input", "expect", "refuses"})
REFUSAL_KEYS = frozenset({"error", "message"})


@dataclass(frozen=True)
class AcceptanceCriterion:
    """One public acceptance criterion, with its declared observation seam.

    A criterion authored as a plain string is advisory: nothing gates on it and
    no seam is required. A criterion authored as an object may declare
    ``gating: true``, which makes it an obligation a tests lane must discharge,
    and a gating obligation must name the ``observation_seam`` a case can assert
    on from the public contract. Gating is declared, never read out of the prose.

    A gating obligation also states its expected answers as ``decided_by``:
    worked examples, each an exact ``input`` with exactly one exact ``expect``
    or ``refuses``. ``restriction`` carries the claim's structure a refusal
    example depends on -- ``polarity``, whether it has ``exception_ids`` or
    ``preconditions``, and whether its witness store is ``external`` (it reads
    an upstream endpoint that can be unavailable) -- and the compiler derives
    from it, itself, whether a
    ``refuses`` example is owed. No boolean is trusted in its place. The
    examples are public by construction and reach every actor verbatim through
    ``public_text``.
    """

    criterion: str
    gating: bool = False
    observation_seam: Optional[str] = None
    decided_by: Any = None
    restriction: Any = None

    @property
    def refusal_required(self) -> bool:
        """Derived from ``restriction``: negative, exceptions, preconditions, or external."""
        restriction = self.restriction
        return isinstance(restriction, dict) and (
            restriction.get("polarity") == "negative"
            or restriction.get("has_exception_ids") is True
            or restriction.get("has_preconditions") is True
            or restriction.get("external_store") is True
        )

    @property
    def public_text(self) -> str:
        """What reaches the tester, builder, and reviewer as acceptance."""
        text = self.criterion
        if self.observation_seam:
            text = "{0} [observable: {1}]".format(text, self.observation_seam)
        if self.decided_by is not None:
            text = "{0} [decided by: {1}]".format(
                text,
                json.dumps(
                    self.decided_by,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            )
        return text

    def canonical(self) -> Any:
        """The authored form, normalized: a plain string stays a plain string."""
        if (
            not self.gating
            and self.observation_seam is None
            and self.decided_by is None
            and self.restriction is None
        ):
            return self.criterion
        payload: dict = {"criterion": self.criterion, "gating": self.gating}
        if self.observation_seam is not None:
            payload["observation_seam"] = self.observation_seam
        if self.decided_by is not None:
            payload["decided_by"] = self.decided_by
        if self.restriction is not None:
            payload["restriction"] = self.restriction
        return payload


def restriction_problems(value: Any) -> Tuple[str, ...]:
    """Whether a gating obligation declares the structure a refusal depends on."""
    if value is None:
        return (
            "declares no restriction (polarity, has_exception_ids, "
            "has_preconditions, external_store), so whether it owes a refuses "
            "example cannot be decided",
        )
    if (
        not isinstance(value, dict)
        or set(value) != RESTRICTION_KEYS
        or value.get("polarity") not in ("positive", "negative")
        or not isinstance(value.get("has_exception_ids"), bool)
        or not isinstance(value.get("has_preconditions"), bool)
        or not isinstance(value.get("external_store"), bool)
    ):
        return (
            "restriction must be exactly polarity (positive|negative), "
            "has_exception_ids, has_preconditions and external_store (bools)",
        )
    return ()


def decided_by_problems(value: Any, *, refusal_required: bool) -> Tuple[str, ...]:
    """What a gating obligation's worked examples leave undecided; empty if none.

    Structural only: an exact input and exactly one exact answer per example,
    at least one ``expect``, and at least one ``refuses`` when the obligation
    declares an input restriction. No prose is inspected.
    """
    if not isinstance(value, list) or not value:
        return ("declares no decided_by worked examples",)
    problems = []
    expects = refuses = 0
    for index, example in enumerate(value):
        if (
            not isinstance(example, dict)
            or set(example) - DECIDED_BY_EXAMPLE_KEYS
            or "input" not in example
            or ("expect" in example) == ("refuses" in example)
        ):
            problems.append(
                "decided_by[{0}] must be an exact input with exactly one of "
                "expect or refuses".format(index)
            )
            continue
        if "expect" in example:
            expects += 1
            continue
        refusal = example["refuses"]
        if (
            not isinstance(refusal, dict)
            or set(refusal) - REFUSAL_KEYS
            or not isinstance(refusal.get("error"), str)
            or not refusal["error"].strip()
            or (
                "message" in refusal
                and (not isinstance(refusal["message"], str) or not refusal["message"].strip())
            )
        ):
            problems.append(
                "decided_by[{0}].refuses must name the exact error and, where "
                "the contract has one, its message".format(index)
            )
            continue
        refuses += 1
    if not problems and not expects:
        problems.append("has no expect example stating an exact correct output")
    if not problems and refusal_required and not refuses:
        problems.append(
            "restricts its input or reads an external store (negative "
            "polarity, exception_ids, preconditions or external_store) but has "
            "no refuses example stating the exact refusal, such as the source "
            "being unavailable"
        )
    return tuple(problems)


def parse_acceptance_item(raw: Any) -> Optional[AcceptanceCriterion]:
    """One acceptance criterion, or None if the authored item is inadmissible.

    Accepts the historical plain-string form and the object form carrying the
    declared ``gating`` flag, ``observation_seam``, ``decided_by`` examples
    and ``restriction``. Example and restriction shape are judged by the compiler,
    which names what is missing (``OBLIGATION_UNDECIDED``). No prose is inspected.
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
    return AcceptanceCriterion(
        criterion, gating, seam, raw.get("decided_by"), raw.get("restriction")
    )


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
