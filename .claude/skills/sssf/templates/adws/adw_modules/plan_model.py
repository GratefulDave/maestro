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
INTERFACE_ENTRY_KEYS = frozenset(
    {"kind", "module", "name", "signature", "errors", "consumed_by"}
)
INTERFACE_KINDS = frozenset({"callable", "route", "component"})
#: A consumer declaration is exactly one of these, never both and never
#: neither: ``lane`` names a lane in this plan whose declared outputs contain
#: the call site, ``deferred_to`` names the sibling work package or plan that
#: will consume the interface.
CONSUMED_BY_KEYS = frozenset({"lane", "deferred_to"})
_INTERFACE_HTTP_METHODS = frozenset(
    {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}
)
_CALLABLE_SIGNATURE_KEYS = frozenset({"parameters", "returns"})
_ROUTE_SIGNATURE_KEYS = frozenset({"method", "path", "params", "response"})
_COMPONENT_SIGNATURE_KEYS = frozenset({"props"})
_PARAMETER_KEYS = frozenset({"name", "type"})


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

def _named_typed_list(value: Any, item_label: str) -> Optional[str]:
    """A list of {name, type} objects; None when well-formed, else the problem."""
    if not isinstance(value, list) or not value:
        return "{0} must be a nonempty array".format(item_label)
    for index, item in enumerate(value):
        if (
            not isinstance(item, dict)
            or set(item) - _PARAMETER_KEYS
            or not isinstance(item.get("name"), str)
            or not item["name"].strip()
            or not isinstance(item.get("type"), str)
            or not item["type"].strip()
        ):
            return (
                "{0}[{1}] must be an object with nonempty name and type".format(
                    item_label, index
                )
            )
    return None


def _signature_problems(kind: str, signature: Any) -> Tuple[str, ...]:
    """The per-kind signature shape an interface entry must declare."""
    if not isinstance(signature, dict):
        return ("signature must be an object",)
    if kind == "callable":
        if set(signature) != _CALLABLE_SIGNATURE_KEYS:
            return (
                "callable signature must be exactly parameters and returns",
            )
        problem = _named_typed_list(signature.get("parameters"), "parameters")
        if problem:
            return (problem,)
        returns = signature.get("returns")
        if not isinstance(returns, str) or not returns.strip():
            return ("returns must be a nonempty return shape",)
        return ()
    if kind == "route":
        if not set(signature) <= _ROUTE_SIGNATURE_KEYS:
            return (
                "route signature may only carry method, path, params, response",
            )
        method = signature.get("method")
        if (
            not isinstance(method, str)
            or method.strip().upper() not in _INTERFACE_HTTP_METHODS
        ):
            return ("route signature.method must be an HTTP method",)
        path = signature.get("path")
        if (
            not isinstance(path, str)
            or not path.strip()
            or not path.strip().startswith("/")
        ):
            return ("route signature.path must be a nonempty path starting with /",)
        if "params" in signature:
            problem = _named_typed_list(signature.get("params"), "params")
            if problem:
                return (problem,)
        response = signature.get("response")
        if (
            not isinstance(response, (str, dict, list))
            or (isinstance(response, str) and not response.strip())
            or (isinstance(response, (dict, list)) and not response)
        ):
            return ("route signature.response must be a nonempty shape",)
        return ()
    if kind == "component":
        if set(signature) != _COMPONENT_SIGNATURE_KEYS:
            return ("component signature must be exactly props",)
        problem = _named_typed_list(signature.get("props"), "props")
        if problem:
            return (problem,)
        return ()
    return ("kind must be callable, route or component",)


def interface_entry_problems(entry: Any) -> Tuple[str, ...]:
    """What one declared interface entry leaves unstated; empty if well-formed.

    Structural only: the module, the export name and kind, the signature with
    argument names/types and return shape (method/path/params/response for a
    route, props for a component), and optional observable errors. No prose is
    inspected.
    """
    if not isinstance(entry, dict):
        return ("interface entry must be an object",)
    extra = set(entry) - INTERFACE_ENTRY_KEYS
    if extra:
        return (
            "interface entry has unknown field(s): {0}".format(
                ", ".join(sorted(extra))
            ),
        )
    kind = entry.get("kind")
    if kind not in INTERFACE_KINDS:
        return ("kind must be callable, route or component",)
    problems = []
    module = entry.get("module")
    if not isinstance(module, str) or not module.strip():
        problems.append("module must be a nonempty module path")
    name = entry.get("name")
    if not isinstance(name, str) or not name.strip():
        problems.append("name must be a nonempty export name")
    problems.extend(_signature_problems(kind, entry.get("signature")))
    errors = entry.get("errors")
    if errors is not None and (
        not isinstance(errors, list)
        or any(not isinstance(item, str) or not item.strip() for item in errors)
    ):
        problems.append("errors must be an array of observable error names")
    return tuple(problems)


def consumer_problems(entry: Any) -> Tuple[str, ...]:
    """What one entry's ``consumed_by`` leaves unstated; empty if well-formed.

    Shape only. Whether a named ``lane`` is a lane of this plan, and whether
    it is the declaring lane itself, needs the lane graph and is judged by
    ``plan_validate`` (``INTERFACE_UNCONSUMED``). No prose is inspected: a
    ``deferred_to`` is required to be nonempty, not to be true.
    """
    if not isinstance(entry, dict):
        return ()
    declared = entry.get("consumed_by")
    if declared is None:
        return (
            "consumed_by is required: name the lane in this plan whose "
            "declared outputs contain the call site, or the sibling work "
            "package the consumption is deferred to",
        )
    if not isinstance(declared, dict):
        return ("consumed_by must be an object",)
    extra = set(declared) - CONSUMED_BY_KEYS
    if extra:
        return (
            "consumed_by has unknown field(s): {0}".format(
                ", ".join(sorted(extra))
            ),
        )
    if len(declared) != 1:
        return ("consumed_by must carry exactly one of lane or deferred_to",)
    if "lane" in declared:
        lane = declared["lane"]
        if not isinstance(lane, str) or not lane.strip():
            return ("consumed_by.lane must be a nonempty lane id",)
        return ()
    deferral = declared["deferred_to"]
    if not isinstance(deferral, str) or not deferral.strip():
        return (
            "consumed_by.deferred_to must name the sibling work package or "
            "plan that will consume this interface",
        )
    return ()


def interface_problems(value: Any) -> Tuple[str, ...]:
    """Whether a lane's declared interface is a well-formed entry list.

    ``None`` means the lane declares no interface; that is legal here and is
    judged separately by the pairing check in ``plan_validate``.
    """
    if value is None:
        return ()
    if not isinstance(value, list):
        return ("interface must be an array of interface entries",)
    problems = []
    for index, entry in enumerate(value):
        for problem in interface_entry_problems(entry):
            problems.append("interface[{0}]: {1}".format(index, problem))
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
