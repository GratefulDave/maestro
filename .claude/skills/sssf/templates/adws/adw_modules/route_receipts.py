"""Verification and admission of captured route execution receipts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import FrozenSet, Mapping, Sequence

from . import receipt_crypto as crypto


class ReceiptInvalid(ValueError):
    """Receipt or admission material cannot authorize a route."""


class ReceiptSignatureMissing(ReceiptInvalid):
    """A receipt exists without its required detached signature."""


class ReceiptSignatureInvalid(ReceiptInvalid):
    """A receipt's detached signature does not verify."""


@dataclass(frozen=True)
class RouteReceipt:
    route: str
    binary_version: str
    requested_model: str
    reported_model: str
    first_exit_code: int
    continuation_exit_code: int
    continuity_proven: bool
    visible_pane_cwd_verified: bool
    cancellation_clean: bool


_ADMISSION_CONSTRUCTION_TOKEN = object()


class AdmittedRouteSet:
    """Immutable routes created only after detached-signature verification."""

    __slots__ = ("_routes",)

    def __init__(
            self, routes: FrozenSet[str], *, _verification_token: object,
    ) -> None:
        if _verification_token is not _ADMISSION_CONSTRUCTION_TOKEN:
            raise TypeError("VERIFIED_ADMITTED_ROUTES_REQUIRED")
        object.__setattr__(self, "_routes", frozenset(routes))

    @property
    def routes(self) -> FrozenSet[str]:
        return self._routes

    def admits(self, route: str) -> bool:
        return route in self._routes

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("VERIFIED_ADMITTED_ROUTES_IMMUTABLE")


def load_public_key(path: Path) -> bytes:
    """Load one explicit hexadecimal Ed25519 verification key."""
    try:
        key = bytes.fromhex(Path(path).read_text(encoding="ascii").strip())
    except (OSError, UnicodeError, ValueError) as exc:
        raise ReceiptInvalid("ROUTE_RECEIPT_KEY_INVALID:{}".format(path)) from exc
    if len(key) != crypto.PUBLIC_KEY_SIZE:
        raise ReceiptInvalid("ROUTE_RECEIPT_KEY_INVALID:{}".format(path))
    return key


def load_route_receipt(path: Path, *, verify_keys: Sequence[bytes]) -> RouteReceipt:
    """Verify detached bytes first, then validate their complete capture."""
    receipt_path = Path(path)
    try:
        data = receipt_path.read_bytes()
    except OSError as exc:
        raise ReceiptInvalid("ROUTE_RECEIPT_UNREADABLE:{}".format(path)) from exc
    signature = _load_signature(receipt_path)
    _verify_signature(data, signature, verify_keys)
    try:
        raw = json.loads(data.decode("utf-8"))
        if not isinstance(raw, dict):
            raise TypeError("receipt must be an object")
        first = _mapping(raw, "first_turn")
        continuation = _mapping(raw, "continuation_turn")
        receipt = RouteReceipt(
            route=_string(raw, "route"),
            binary_version=_string(raw, "binary_version"),
            requested_model=_string(raw, "requested_model"),
            reported_model=_string(raw, "reported_model"),
            first_exit_code=_exit_code(first),
            continuation_exit_code=_exit_code(continuation),
            continuity_proven=raw["continuity_proven"] is True,
            visible_pane_cwd_verified=raw["visible_pane_cwd_verified"] is True,
            cancellation_clean=raw["cancellation_clean"] is True,
        )
        _string(raw, "schema")
        _string(raw, "captured_at")
        _string(first, "event_type")
        _string(first, "text")
        _string(continuation, "continued_with")
        _string(continuation, "question")
        _string(continuation, "text")
    except (KeyError, TypeError, UnicodeError, ValueError,
            json.JSONDecodeError) as exc:
        raise ReceiptInvalid("ROUTE_RECEIPT_INCOMPLETE:{}".format(path)) from exc
    if raw["schema"] != "maestro-route-receipt.v1":
        raise ReceiptInvalid("ROUTE_RECEIPT_SCHEMA")
    if receipt.route not in ("omp", "claude"):
        raise ReceiptInvalid("ROUTE_NOT_ADMITTED")
    try:
        _validate_capture(raw, first, continuation, receipt)
    except (KeyError, TypeError) as exc:
        raise ReceiptInvalid(
            "ROUTE_RECEIPT_INCOMPLETE:{}".format(path)) from exc
    if receipt.first_exit_code != 0 or receipt.continuation_exit_code != 0:
        raise ReceiptInvalid("ROUTE_EXECUTION_FAILED")
    if not receipt.continuity_proven:
        raise ReceiptInvalid("ROUTE_CONTINUITY_UNPROVEN")
    if not receipt.visible_pane_cwd_verified:
        raise ReceiptInvalid("ROUTE_CWD_UNPROVEN")
    if not receipt.cancellation_clean:
        raise ReceiptInvalid("ROUTE_CANCELLATION_UNPROVEN")
    return receipt


def load_admitted_routes(
        receipt_paths: Mapping[str, Path], *, verify_keys: Sequence[bytes],
) -> AdmittedRouteSet:
    """Bind each configured route name to matching signed receipt bytes."""
    routes = set()
    for expected_route, path in receipt_paths.items():
        if expected_route not in ("omp", "claude"):
            raise ReceiptInvalid("ROUTE_NOT_ADMITTED:{}".format(expected_route))
        receipt = load_route_receipt(path, verify_keys=verify_keys)
        if receipt.route != expected_route:
            raise ReceiptInvalid(
                "ROUTE_RECEIPT_ROUTE_MISMATCH:{}!={}".format(
                    receipt.route, expected_route))
        routes.add(receipt.route)
    return AdmittedRouteSet(
        frozenset(routes), _verification_token=_ADMISSION_CONSTRUCTION_TOKEN)


def _load_signature(receipt_path: Path) -> bytes:
    signature_path = Path(str(receipt_path) + ".sig")
    try:
        encoded = signature_path.read_text(encoding="ascii").strip()
    except FileNotFoundError as exc:
        raise ReceiptSignatureMissing(
            "ROUTE_RECEIPT_SIGNATURE_MISSING:{}".format(receipt_path)) from exc
    except (OSError, UnicodeError) as exc:
        raise ReceiptSignatureInvalid(
            "ROUTE_RECEIPT_SIGNATURE_INVALID:{}".format(receipt_path)) from exc
    try:
        signature = bytes.fromhex(encoded)
    except ValueError as exc:
        raise ReceiptSignatureInvalid(
            "ROUTE_RECEIPT_SIGNATURE_INVALID:{}".format(receipt_path)) from exc
    if len(signature) != crypto.SIGNATURE_SIZE:
        raise ReceiptSignatureInvalid(
            "ROUTE_RECEIPT_SIGNATURE_INVALID:{}".format(receipt_path))
    return signature


def _verify_signature(
        data: bytes, signature: bytes, verify_keys: Sequence[bytes],
) -> None:
    keys = tuple(verify_keys)
    if not keys:
        raise ReceiptInvalid("ROUTE_RECEIPT_KEY_REQUIRED")
    try:
        verified = any(crypto.verify(key, data, signature) for key in keys)
    except (TypeError, crypto.KeyMaterialError) as exc:
        raise ReceiptInvalid("ROUTE_RECEIPT_KEY_INVALID") from exc
    if not verified:
        raise ReceiptSignatureInvalid("ROUTE_RECEIPT_SIGNATURE_INVALID")


def _mapping(raw: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = raw[key]
    if not isinstance(value, dict):
        raise TypeError("{} must be an object".format(key))
    return value


def _string(raw: Mapping[str, object], key: str) -> str:
    value = raw[key]
    if not isinstance(value, str) or not value:
        raise TypeError("{} must be a nonempty string".format(key))
    return value


def _exit_code(raw: Mapping[str, object]) -> int:
    value = raw["exit_code"]
    if type(value) is not int:
        raise TypeError("exit_code must be an integer")
    return value


def _validate_capture(
        raw: Mapping[str, object], first: Mapping[str, object],
        continuation: Mapping[str, object], receipt: RouteReceipt,
) -> None:
    if _string(first, "text") != _string(continuation, "text"):
        raise ReceiptInvalid("ROUTE_CONTINUITY_UNPROVEN")
    if receipt.route == "omp":
        _string(first, "role")
        _string(first, "stop_reason")
        return
    _string(raw, "session_id")
    _string(first, "subtype")
    if first.get("is_error") is not False:
        raise ReceiptInvalid("ROUTE_EXECUTION_FAILED")
