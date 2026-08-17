"""Visible Herdr route admission: capture, sign, and persist receipts.

A route is admitted only by executed evidence. This module talks to Herdr
directly — it cannot use `HerdrLauncher`, which requires an already-admitted
route set. Incomplete capture writes no receipt.
"""

from __future__ import annotations

import json
import uuid
import os
import re
import stat
import subprocess
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Mapping, Optional, Sequence, Tuple

from . import launcher
from . import receipt_crypto as crypto
from . import route_receipts


class AdmissionError(ValueError):
    """Visible route capture cannot produce an admitting receipt."""


MARKERS = {
    "omp": "MAESTRO_OMP_RECEIPT_OK",
    "claude": "MAESTRO_CLAUDE_RECEIPT_OK",
}

FIRST_PROMPT = "Reply with exactly {marker} and nothing else."
CONTINUATION_QUESTION = "previous exact marker"
CONTINUATION_PROMPT = (
    "Reply with the previous exact marker and nothing else."
)

_KEY_FILES = {
    "signing_seed": "signing.seed",
    "signing_pub": "signing.pub",
    "route_seed": "route.seed",
    "route_pub": "route.pub",
}


@dataclass(frozen=True)
class KeyMaterial:
    signing_seed: bytes
    signing_public: bytes
    route_seed: bytes
    route_public: bytes
    keys_dir: Path
    env_file: Path
    created: Tuple[str, ...]


@dataclass(frozen=True)
class RouteCaptureSpec:
    route: str
    cwd: Path
    herdr: Path
    binary: Path
    model: str
    effort: str
    profile: Optional[str]
    session_dir: Path
    timeout_s: float
    startup_settle_s: float = 2.0


@dataclass(frozen=True)
class WrittenReceipt:
    route: str
    path: Path
    reused: bool


def provision_keys(keys_dir: Path) -> KeyMaterial:
    """Create or reuse Ed25519 material under a 0700 keys directory."""
    directory = Path(keys_dir)
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, stat.S_IRWXU)
    created = []

    def load_or_mint(name: str, size: int, mint: Callable[[], bytes]) -> bytes:
        path = directory / name
        if path.is_file():
            try:
                material = bytes.fromhex(path.read_text(encoding="ascii").strip())
            except (OSError, UnicodeError, ValueError) as exc:
                raise AdmissionError(
                    "KEY_MATERIAL_INVALID:{}".format(path)) from exc
            if len(material) != size:
                raise AdmissionError("KEY_MATERIAL_INVALID:{}".format(path))
            return material
        material = mint()
        _write_secret(path, material.hex())
        created.append(name)
        return material

    signing_seed = load_or_mint(
        _KEY_FILES["signing_seed"], crypto.SEED_SIZE, crypto.generate_seed)
    route_seed = load_or_mint(
        _KEY_FILES["route_seed"], crypto.SEED_SIZE, crypto.generate_seed)
    signing_public = crypto.seed_to_public_key(signing_seed)
    route_public = crypto.seed_to_public_key(route_seed)
    for name, public in (
            (_KEY_FILES["signing_pub"], signing_public),
            (_KEY_FILES["route_pub"], route_public)):
        path = directory / name
        if not path.is_file():
            _write_public(path, public.hex())
            created.append(name)
    return KeyMaterial(
        signing_seed=signing_seed,
        signing_public=signing_public,
        route_seed=route_seed,
        route_public=route_public,
        keys_dir=directory,
        env_file=directory / "maestro.env",
        created=tuple(created),
    )


def write_env_file(
        keys: KeyMaterial, *,
        verify_key_env: str, signing_seed_env: str,
        route_verify_key_env: str,
) -> Path:
    """Write the three operator environment bindings (0600)."""
    body = (
        "{verify}={verify_hex}\n"
        "{seed}={seed_hex}\n"
        "{route}={route_hex}\n"
    ).format(
        verify=verify_key_env,
        verify_hex=keys.signing_public.hex(),
        seed=signing_seed_env,
        seed_hex=keys.signing_seed.hex(),
        route=route_verify_key_env,
        route_hex=keys.route_public.hex(),
    )
    _write_secret(keys.env_file, body)
    return keys.env_file


def encode_receipt(receipt: Mapping[str, object]) -> bytes:
    """Stable receipt bytes. The detached signature covers these exact bytes."""
    return (json.dumps(receipt, sort_keys=True, indent=2, ensure_ascii=False)
            + "\n").encode("utf-8")


def write_signed_receipt(path: Path, receipt: Mapping[str, object],
                         seed: bytes) -> Tuple[bytes, bytes]:
    """Write receipt JSON and a detached hex signature. No overwrite."""
    destination = Path(path)
    signature_path = Path(str(destination) + ".sig")
    if destination.exists() or signature_path.exists():
        raise AdmissionError("ROUTE_RECEIPT_EXISTS:{}".format(destination))
    data = encode_receipt(receipt)
    signature = crypto.sign(seed, data)
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_name(destination.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, destination)
    _write_public(signature_path, signature.hex())
    return data, signature


def existing_receipt_is_admitted(
        path: Path, *, verify_keys: Sequence[bytes], route: str,
) -> bool:
    """True only when the on-disk pair already admits `route`."""
    try:
        receipt = route_receipts.load_route_receipt(
            path, verify_keys=verify_keys)
    except route_receipts.ReceiptInvalid:
        return False
    return receipt.route == route


def admit_routes(
        specs: Sequence[RouteCaptureSpec],
        destinations: Mapping[str, Path],
        *,
        route_seed: bytes,
        herdr: Optional[Callable[..., dict]] = None,
        version_of: Optional[Callable[[Path], str]] = None,
        clock: Callable[[], str] = lambda: datetime.now(timezone.utc).date().isoformat(),
) -> Tuple[WrittenReceipt, ...]:
    """Capture each missing route and persist a signed receipt."""
    written = []
    verify_keys = (crypto.seed_to_public_key(route_seed),)
    for spec in specs:
        if spec.route not in MARKERS:
            raise AdmissionError("ROUTE_NOT_ADMITTED:{}".format(spec.route))
        path = Path(destinations[spec.route])
        if existing_receipt_is_admitted(
                path, verify_keys=verify_keys, route=spec.route):
            written.append(WrittenReceipt(spec.route, path, True))
            continue
        receipt = capture_route(
            spec, herdr=herdr, version_of=version_of, clock=clock)
        write_signed_receipt(path, receipt, route_seed)
        route_receipts.load_route_receipt(path, verify_keys=verify_keys)
        written.append(WrittenReceipt(spec.route, path, False))
    return tuple(written)


def capture_route(
        spec: RouteCaptureSpec,
        *,
        herdr: Optional[Callable[..., dict]] = None,
        version_of: Optional[Callable[[Path], str]] = None,
        clock: Callable[[], str] = lambda: datetime.now(timezone.utc).date().isoformat(),
) -> Dict[str, object]:
    """Execute first turn + continuation in a visible pane and prove cancel."""
    if spec.route not in MARKERS:
        raise AdmissionError("ROUTE_NOT_ADMITTED:{}".format(spec.route))
    call = herdr or (lambda *args, timeout=None: _herdr(spec.herdr, *args, timeout=timeout))
    _require_herdr_session(call)
    cwd = Path(spec.cwd).resolve()
    spec.session_dir.mkdir(parents=True, exist_ok=True)
    capture_dir = spec.session_dir / uuid.uuid4().hex
    capture_dir.mkdir()
    spec = replace(spec, session_dir=capture_dir)
    marker = MARKERS[spec.route]
    timeout_ms = str(max(1, int(spec.timeout_s * 1000)))
    first_handle = None
    continuation_handle = None
    try:
        first_handle = _start_visible_agent(call, spec, cwd, continuing=False)
        first_prompt = FIRST_PROMPT.format(marker=marker)
        first_records = _prompt_turn(
            call, first_handle, first_prompt, timeout_ms, marker)
        first_turn, session_id, reported = _parse_turn(
            spec.route, first_records, marker, first_prompt)
        if spec.route == "claude" and not session_id:
            session_id = _session_from_agent(call, first_handle["name"])
        _stop_agent(call, first_handle)
        first_handle = None
        continuation_handle = _start_visible_agent(
            call, spec, cwd, continuing=True, session_id=session_id)
        continued_with = "-c" if spec.route == "omp" else "--resume"
        continuation_records = _prompt_turn(
            call, continuation_handle, CONTINUATION_PROMPT, timeout_ms, marker)
        continuation, _, _ = _parse_turn(
            spec.route, continuation_records, marker, CONTINUATION_PROMPT)
        if continuation["text"] != first_turn["text"]:
            raise AdmissionError("ROUTE_CONTINUITY_UNPROVEN")
        _stop_agent(call, continuation_handle)
        gone = _agent_gone(call, continuation_handle["name"])
        continuation_handle = None
        if not gone:
            raise AdmissionError("ROUTE_CANCELLATION_UNPROVEN")
    finally:
        if first_handle is not None:
            _best_effort_close(call, first_handle)
        if continuation_handle is not None:
            _best_effort_close(call, continuation_handle)
    version = (version_of or binary_version)(spec.binary)
    receipt: Dict[str, object] = {
        "schema": "maestro-route-receipt.v1",
        "captured_at": clock(),
        "route": spec.route,
        "binary_version": version,
        "requested_model": spec.model,
        "reported_model": reported or spec.model,
        "first_turn": first_turn,
        "continuation_turn": {
            "continued_with": continued_with,
            "question": CONTINUATION_QUESTION,
            "text": continuation["text"],
            "exit_code": continuation["exit_code"],
        },
        "continuity_proven": True,
        "visible_pane_cwd_verified": True,
        "cancellation_clean": True,
    }
    if spec.route == "claude":
        if not session_id:
            raise AdmissionError("ROUTE_RECEIPT_INCOMPLETE")
        receipt["session_id"] = session_id
        receipt["continuation_turn"]["is_error"] = False
    return receipt


def binary_version(binary: Path) -> str:
    """First line of `<binary> --version`, refusing an empty report."""
    try:
        result = subprocess.run(
            [str(binary), "--version"], capture_output=True, text=True,
            timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AdmissionError("BINARY_VERSION_UNAVAILABLE:{}".format(binary)) from exc
    line = (result.stdout or result.stderr).strip().splitlines()
    if result.returncode != 0 or not line or not line[0].strip():
        raise AdmissionError("BINARY_VERSION_UNAVAILABLE:{}".format(binary))
    return line[0].strip()


def _require_herdr_session(call: Callable[..., dict]) -> None:
    try:
        payload = call("pane", "current", "--current")
    except AdmissionError as exc:
        raise AdmissionError("HERDR_SESSION_REQUIRED") from exc
    pane = _extract(payload, "pane")
    if not isinstance(pane, dict) or not pane.get("pane_id"):
        raise AdmissionError("HERDR_SESSION_REQUIRED")


def _start_visible_agent(
        call: Callable[..., dict], spec: RouteCaptureSpec, cwd: Path,
        *, continuing: bool, session_id: Optional[str] = None,
) -> Dict[str, str]:
    split = call(
        "pane", "split", "--current", "--direction", "right",
        "--cwd", str(cwd), "--no-focus")
    pane = _extract(split, "pane")
    if not isinstance(pane, dict) or not pane.get("pane_id"):
        raise AdmissionError("LAUNCH_REFUSED:NO_PANE")
    pane_id = str(pane["pane_id"])
    _assert_pane_cwd(call, pane_id, cwd)
    try:
        _wait_for_available_shell(call, pane_id)
        # A split pane can report zsh before its startup hooks finish. Let the
        # settled shell remain stable before starting the route CLI.
        time.sleep(1.0)
    except AdmissionError:
        _best_effort_close(call, {"pane_id": pane_id, "name": ""})
        raise
    name = "admit-{}-{}".format(spec.route, "cont" if continuing else "first")
    argv = _route_argv(spec, continuing=continuing, session_id=session_id)
    start_deadline = time.monotonic() + 60.0
    while True:
        try:
            started = call(
                "agent", "start", name, "--kind", spec.route,
                "--pane", pane_id,
                "--timeout", "180000",
                "--", *argv[1:],
                timeout=185.0)
            break
        except AdmissionError as exc:
            if "agent_pane_busy" not in str(exc) or time.monotonic() >= start_deadline:
                _best_effort_close(call, {"pane_id": pane_id, "name": name})
                raise
            try:
                _wait_for_available_shell(
                    call, pane_id,
                    timeout_s=min(5.0, max(0.1, start_deadline - time.monotonic())))
            except AdmissionError:
                if time.monotonic() >= start_deadline:
                    _best_effort_close(call, {"pane_id": pane_id, "name": name})
                    raise
            time.sleep(0.5)
    _assert_pane_cwd(call, pane_id, cwd)
    # `agent start` returns once the agent owns the terminal, but the composer
    # can still be busy drawing its first screen. Wait for the documented idle
    # state before submitting a prompt.
    launcher.wait_for_interactive_agent(
        call, name, timeout_s=spec.timeout_s)
    agent = _extract(started, "agent")
    transcript = ""
    if isinstance(agent, dict) and agent.get("transcript_path"):
        transcript = str(agent["transcript_path"])
    return {"pane_id": pane_id, "name": name, "transcript": transcript}


def _prompt_turn(
        call: Callable[..., dict], handle: Mapping[str, str],
        prompt: str, timeout_ms: str, marker: str,
) -> Tuple[dict, ...]:
    timeout_s = max(0.001, int(timeout_ms) / 1000.0)
    try:
        # The admission turn is a single short answer, so wait for the agent to
        # settle back at idle: that is the proof the prompt was submitted and
        # answered rather than left sitting in the composer.
        launcher.submit_agent_prompt(
            call, handle["pane_id"], prompt, handle["name"],
            timeout_s=timeout_s)
    except RuntimeError as exc:
        raise AdmissionError(str(exc)) from exc
    deadline = time.monotonic() + timeout_s
    while True:
        records = list(_transcript_records(handle.get("transcript") or ""))
        if not _reply_marker_present(records, marker, prompt):
            for source in ("recent-unwrapped", "visible", "detection"):
                try:
                    read = call(
                        "pane", "read", handle["pane_id"],
                        "--source", source, "--lines", "120")
                except AdmissionError:
                    continue
                records.extend(_embedded_records(read))
                text = _pane_text(read)
                if text:
                    records.append({"text": text})
                if _reply_marker_present(records, marker, prompt):
                    break
        if _reply_marker_present(records, marker, prompt):
            return tuple(records)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AdmissionError("ROUTE_RECEIPT_INCOMPLETE")
        time.sleep(min(0.05, remaining))


def _pane_text(payload: Mapping[str, object]) -> str:
    stack = [payload]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            for key in ("text", "output", "content"):
                value = current.get(key)
                if isinstance(value, str) and value.strip():
                    return value
            lines = current.get("lines")
            if isinstance(lines, list):
                joined = "\n".join(
                    str(line) for line in lines if line is not None)
                if joined.strip():
                    return joined
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
    return ""


def _parse_turn(
        route: str, records: Sequence[Mapping[str, object]], marker: str,
        prompt: str = "",
        ) -> Tuple[Dict[str, object], str, str]:
    text = _first_text(records, marker, prompt)
    if not text:
        raise AdmissionError("ROUTE_RECEIPT_INCOMPLETE")
    reported = _first_string(records, "model") or _first_string(records, "reported_model")
    session_id = _first_string(records, "session_id") or _session_from_text(records)
    exit_code = _first_int(records, "exit_code")
    if exit_code is None:
        exit_code = 0
    if exit_code != 0:
        raise AdmissionError("ROUTE_EXECUTION_FAILED")
    if route == "omp":
        turn = {
            "event_type": _first_string(records, "event_type") or "message_end",
            "role": _first_string(records, "role") or "assistant",
            "text": text,
            "stop_reason": _first_string(records, "stop_reason") or "stop",
            "exit_code": exit_code,
        }
        return turn, session_id, reported
    if _first_value(records, "is_error") is True:
        raise AdmissionError("ROUTE_EXECUTION_FAILED")
    turn = {
        "event_type": _first_string(records, "event_type") or "result",
        "subtype": _first_string(records, "subtype") or "success",
        "text": text,
        "is_error": False,
        "exit_code": exit_code,
    }
    return turn, session_id, reported


def _route_argv(
        spec: RouteCaptureSpec, *, continuing: bool,
        session_id: Optional[str],
) -> Tuple[str, ...]:
    launch = launcher.LaunchSpec(
        correlation_token="admit-{}".format(spec.route),
        worktree=spec.cwd,
        prompt_path=spec.session_dir / "unused.prompt",
        envelope_path=spec.session_dir / "unused.envelope",
        route=spec.route,
        model=spec.model,
        effort=spec.effort,
        profile=spec.profile,
        session_dir=spec.session_dir,
    )
    if spec.route == "omp":
        if not spec.profile:
            raise AdmissionError("OMP_PROFILE_REQUIRED")
        argv = list(launcher.build_omp_argv(spec.binary, launch))
        if continuing and "-c" not in argv:
            argv.append("-c")
        return tuple(argv)
    argv = list(launcher.build_claude_argv(spec.binary, launch))
    if continuing:
        if not session_id:
            raise AdmissionError("ROUTE_RECEIPT_INCOMPLETE")
        argv.extend(["--resume", session_id])
    return tuple(argv)


def _assert_pane_cwd(call: Callable[..., dict], pane_id: str, cwd: Path) -> None:
    current = call("pane", "get", pane_id)
    bound = _extract(current, "pane")
    actual = (Path(str(bound.get("cwd"))).resolve()
              if isinstance(bound, dict) and bound.get("cwd") else None)
    if actual != cwd:
        raise AdmissionError("ROUTE_CWD_UNPROVEN:{}!={}".format(actual, cwd))


def _wait_for_available_shell(
        call: Callable[..., dict], pane_id: str, timeout_s: float = 30.0,
        settle_polls: int = 5,
) -> None:
    """Wait until the pane is a settled interactive shell.

    A single ready snapshot is not enough: a freshly split pane can look like a
    lone zsh in the gap before login hooks (direnv, keychain lookups) spawn
    their own foreground processes. Starting an agent in that gap makes Herdr
    report `agent_pane_busy`. Require several consecutive ready snapshots so the
    pane has demonstrably stopped changing before we start the agent.
    """
    deadline = time.monotonic() + timeout_s
    ready = 0
    while True:
        try:
            payload = call("pane", "process-info", "--pane", pane_id)
        except AdmissionError:
            payload = {}
        ready = ready + 1 if _available_shell(payload) else 0
        if ready >= settle_polls:
            return
        if time.monotonic() >= deadline:
            raise AdmissionError("LAUNCH_REFUSED:SHELL_NOT_READY")
        time.sleep(0.1)


def _available_shell(payload: Mapping[str, object]) -> bool:
    info = _extract(payload, "process_info")
    if not isinstance(info, dict):
        return False
    procs = info.get("foreground_processes")
    if not isinstance(procs, list) or len(procs) != 1:
        return False
    proc = procs[0]
    if not isinstance(proc, dict):
        return False
    shell_pid = _optional_int(info.get("shell_pid"))
    pgid = _optional_int(info.get("foreground_process_group_id"))
    proc_pid = _optional_int(proc.get("pid"))
    if None not in (shell_pid, pgid, proc_pid) and not (
            shell_pid == pgid == proc_pid):
        return False
    token = str(proc.get("name") or proc.get("argv0") or "")
    argv = proc.get("argv")
    if isinstance(argv, list) and argv:
        token = token or str(argv[0] or "")
    base = token.rsplit("/", 1)[-1].lstrip("-").lower()
    return base in ("zsh", "bash", "sh", "fish", "dash", "ksh", "tcsh", "csh")


def _optional_int(value: object) -> Optional[int]:
    if type(value) is int:
        return value
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _stop_agent(call: Callable[..., dict], handle: Mapping[str, str]) -> None:
    """Close the pane and prove it is gone.

    `herdr pane close` answers with the generic success envelope
    (`{"result": {"type": "ok"}}`); the API schema carries no `closed` field for
    it. Absence of the pane afterwards is the only real evidence, so ask for it.
    """
    try:
        call("pane", "close", handle["pane_id"])
    except AdmissionError:
        pass
    if not _pane_gone(call, handle["pane_id"]):
        raise AdmissionError("ROUTE_CANCELLATION_UNPROVEN")


def _pane_gone(call: Callable[..., dict], pane_id: str) -> bool:
    try:
        payload = call("pane", "get", pane_id)
    except AdmissionError:
        return True
    return not isinstance(_extract(payload, "pane"), dict)


def _agent_gone(call: Callable[..., dict], name: str) -> bool:
    try:
        payload = call("agent", "get", name)
    except AdmissionError:
        return True
    agent = _extract(payload, "agent")
    return not isinstance(agent, dict)


def _best_effort_close(call: Callable[..., dict], handle: Mapping[str, str]) -> None:
    try:
        if handle.get("pane_id"):
            call("pane", "close", handle["pane_id"])
    except AdmissionError:
        return


def _herdr(herdr: Path, *args: str, timeout: Optional[float] = None) -> dict:
    try:
        result = subprocess.run(
            [str(herdr), *args], capture_output=True, text=True,
            timeout=30 if timeout is None else timeout, check=False)
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        raise AdmissionError("LAUNCH_REFUSED:{}".format(exc)) from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[-400:]
        raise AdmissionError("LAUNCH_REFUSED:{}".format(detail))
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        if _is_text_read(args):
            return {"result": {"text": result.stdout or ""}}
        raise AdmissionError("PROTOCOL_INVALID_JSON") from exc
    if not isinstance(payload, dict):
        if _is_text_read(args):
            return {"result": {"text": result.stdout or ""}}
        raise AdmissionError("PROTOCOL_INVALID_RESPONSE")
    return payload


def _is_text_read(args: Sequence[str]) -> bool:
    """`herdr agent read` / `herdr pane read` print the snapshot as raw text.

    They have no JSON output mode (`--format` accepts only `text` and `ansi`),
    so a JSON decode failure on those commands is the normal case, not a
    protocol violation.
    """
    return len(args) >= 2 and args[0] in ("agent", "pane") and args[1] == "read"


def _extract(payload: Mapping[str, object], key: str) -> object:
    result = payload.get("result", payload)
    if isinstance(result, dict):
        if key in result:
            return result[key]
        for value in result.values():
            if isinstance(value, dict) and key in value:
                return value[key]
    return None


def _transcript_records(path: str) -> Tuple[dict, ...]:
    if not path:
        return ()
    file_path = Path(path)
    if not file_path.is_file():
        return ()
    parsed = []
    for raw in file_path.read_bytes().splitlines():
        if not raw:
            continue
        try:
            record = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(record, dict):
            parsed.append(record)
    return tuple(parsed)

def _embedded_records(payload: Mapping[str, object]) -> Tuple[dict, ...]:
    found = []
    stack = [payload]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            if any(key in current for key in (
                    "text", "event_type", "session_id", "model")):
                found.append(current)
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
    return tuple(found)



_SESSION_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE)


def _session_from_text(records: Sequence[Mapping[str, object]]) -> str:
    for record in records:
        text = record.get("text")
        if not isinstance(text, str):
            continue
        match = _SESSION_RE.search(text)
        if match:
            return match.group(0)
    return ""


def _session_from_agent(call: Callable[..., dict], name: str) -> str:
    try:
        payload = call("agent", "get", name)
    except AdmissionError:
        return ""
    agent = _extract(payload, "agent")
    if not isinstance(agent, dict):
        return ""
    session = agent.get("agent_session")
    if isinstance(session, dict):
        value = session.get("value")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _reply_marker_present(
        records: Sequence[Mapping[str, object]], marker: str, prompt: str,
) -> bool:
    return bool(_first_text(records, marker, prompt))


def _first_text(
        records: Sequence[Mapping[str, object]], marker: str, prompt: str = "",
) -> str:
    for record in records:
        text = record.get("text")
        if isinstance(text, str) and marker in _strip_prompt(text, prompt):
            return marker
    return ""


def _strip_prompt(text: str, prompt: str) -> str:
    """Remove the outbound prompt from pane text before scanning for a marker.

    The prompt is echoed back by the agent's composer, and the admission prompt
    contains the marker itself, so an unstripped echo reads as a completed turn.
    A plain `str.replace` is not enough: the terminal wraps and decorates the
    echo, so the on-screen copy rarely matches the string we sent. Match the
    prompt's words with arbitrary whitespace (including newlines) between them.
    """
    words = prompt.split()
    if not words:
        return text
    pattern = r"\s*".join(re.escape(word) for word in words)
    return re.sub(pattern, " ", text)



def _first_string(records: Sequence[Mapping[str, object]], key: str) -> str:
    value = _first_value(records, key)
    return value if isinstance(value, str) and value else ""


def _first_int(records: Sequence[Mapping[str, object]], key: str) -> Optional[int]:
    value = _first_value(records, key)
    return value if type(value) is int else None


def _first_value(records: Sequence[Mapping[str, object]], key: str) -> object:
    for record in records:
        if key in record:
            return record[key]
    return None


def _write_secret(path: Path, text: str) -> None:
    path.write_text(text if text.endswith("\n") else text + "\n", encoding="ascii")
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def _write_public(path: Path, text: str) -> None:
    path.write_text(text if text.endswith("\n") else text + "\n", encoding="ascii")
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
