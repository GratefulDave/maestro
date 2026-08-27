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
    """Visible route capture cannot produce an admitting receipt.

    `code` carries Herdr's own `error.code` when the refusal came from a Herdr
    call. §1.2 forbids branching on prose, so every retry decision in this
    module reads that field and never the message.
    """

    def __init__(self, message: str, code: str = "") -> None:
        super().__init__(message)
        self.code = code


MARKERS = {
    "omp": "MAESTRO_OMP_RECEIPT_OK",
    "claude": "MAESTRO_CLAUDE_RECEIPT_OK",
}

FIRST_PROMPT = "Reply with exactly {marker} and nothing else."
CONTINUATION_QUESTION = "previous exact marker"
CONTINUATION_PROMPT = (
    "Reply with the previous exact marker and nothing else."
)

#: Herdr's refusal when the requested agent name is already registered.
AGENT_NAME_TAKEN = "agent_name_taken"

#: Herdr's refusal when the target pane is not back at its shell prompt.
AGENT_PANE_BUSY = "agent_pane_busy"

#: How many names one capture may ask for before giving up. The discriminator
#: makes the first name collision-free by construction, so this only has to
#: cover a same-instant collision between two installations.
NAME_ATTEMPTS = 3


def herdr_error_code(text: str) -> str:
    """Herdr's `error.code` from a refused call's output, or `""`.

    Herdr answers a refused call with a single JSON object,
    `{"error": {"code": ..., "message": ...}}`. The code is a typed field; the
    message is free text Herdr may reword at any release. §1.2 forbids keying a
    decision on prose, so every retry branch below reads this and never the
    message.

    Anything that does not parse, or parses without an `error.code`, yields the
    empty string rather than a guess: an unrecognised refusal must not be
    mistaken for a recognised one.
    """
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    error = payload.get("error")
    if not isinstance(error, dict):
        return ""
    code = error.get("code")
    return code if isinstance(code, str) else ""


# The plan-contract reviewer key lives with the rest of the state root's key
# material so an operator never generates or types one. planctl requires at
# least 32 UTF-8 bytes; a 32-byte seed rendered as hex is 64 characters.
REVIEWER_HMAC_KEY_FILE = "reviewer-hmac.key"
REVIEWER_HMAC_KEY_ENV = "PLANCTL_REVIEWER_HMAC_KEY"

# Two environment files, along the same line `plan gate` and `plan review` are
# split along. `maestro.env` is the author's: the verify key, the signing seed,
# and the route verify key, which is everything author-side work needs and
# nothing that can make a gate refuse. `reviewer-hmac.env` is the reviewer's
# and carries one binding.
#
# They were one file, and that defeated the separation the gate check exists to
# prove. An operator has to source the author file to finalize or start a run;
# sourcing it also exported `PLANCTL_REVIEWER_HMAC_KEY`, so `maestro plan gate`
# then refused their own plan with `REVIEWER_KEY_PRESENT` -- correctly, because
# the author side must not hold the key that authorizes its own plan. The only
# way forward was unsetting the variable, gating, re-exporting it, reviewing,
# and unsetting it again, by hand, between stages. That is where the key got
# lost, and Maestro's own bootstrap was what put it in the author's shell.
OPERATOR_ENV_FILE = "maestro.env"
REVIEWER_ENV_FILE = "reviewer-hmac.env"

_KEY_FILES = {
    "signing_seed": "signing.seed",
    "signing_pub": "signing.pub",
    "route_seed": "route.seed",
    "route_pub": "route.pub",
    "reviewer_hmac": REVIEWER_HMAC_KEY_FILE,
}


@dataclass(frozen=True)
class KeyMaterial:
    signing_seed: bytes
    signing_public: bytes
    route_seed: bytes
    route_public: bytes
    reviewer_hmac: bytes
    keys_dir: Path
    env_file: Path
    reviewer_env_file: Path
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
    # Minted once and never regenerated: a new reviewer key would silently
    # invalidate every approval receipt already signed with the old one.
    reviewer_hmac = load_or_mint(
        _KEY_FILES["reviewer_hmac"], crypto.SEED_SIZE, crypto.generate_seed)
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
        reviewer_hmac=reviewer_hmac,
        keys_dir=directory,
        env_file=directory / OPERATOR_ENV_FILE,
        reviewer_env_file=directory / REVIEWER_ENV_FILE,
        created=tuple(created),
    )


def write_env_file(
        keys: KeyMaterial, *,
        verify_key_env: str, signing_seed_env: str,
        route_verify_key_env: str,
) -> Path:
    """Write the author-side operator bindings (0600).

    Author-side, and structurally so rather than by discipline: this function
    takes no parameter that could name the reviewer binding, so the file an
    operator sources before `finalize` or `start` cannot carry the key that
    would make `maestro plan gate` refuse. The reviewer's binding has its own
    file -- `write_reviewer_env_file`.
    """
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


def write_reviewer_env_file(
        keys: KeyMaterial, *,
        reviewer_hmac_key_env: str = REVIEWER_HMAC_KEY_ENV,
) -> Path:
    """Write the reviewer binding (0600), alone, in its own file.

    Nothing in Maestro's supported path reads this file. `maestro plan review`
    resolves the key from `<keys>/reviewer-hmac.key` and injects it into the
    `planctl review` subprocess itself, so a reviewer using Maestro never needs
    an environment at all. The file exists for the one case Maestro does not
    drive: the plan-contract skill running `planctl review` directly, which is
    what `/arch-review` does for an architecture IR, against a `planctl` that
    reads the key from its environment and has no other way to be given one.

    It is a separate file so that sourcing the author's environment cannot pick
    it up, and it says what sourcing it costs, because the operator who does so
    deliberately is the one person who has to know.
    """
    body = (
        "# Reviewer-only. Do NOT source this in an authoring shell: while this\n"
        "# variable is set, `maestro plan gate` refuses with\n"
        "# REVIEWER_KEY_PRESENT, because the author side must not hold the key\n"
        "# that authorizes its own plan. `maestro plan review` injects the key\n"
        "# itself and never reads this file; source it only to drive `planctl\n"
        "# review` directly, in a shell that gates nothing.\n"
        "{reviewer}={reviewer_hex}\n"
    ).format(
        reviewer=reviewer_hmac_key_env,
        reviewer_hex=keys.reviewer_hmac.hex(),
    )
    _write_secret(keys.reviewer_env_file, body)
    return keys.reviewer_env_file


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
    # Validate immutable route requirements before creating a capture directory
    # or asking Herdr to split a pane.
    _route_argv(spec, continuing=False, session_id=None)
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
        # NO team prefix on an admission turn.
        #
        # `prepare_route_prompt_text` prepends a standing instruction to spawn
        # subagents via `/team`. That is right for a lane doing real work and
        # wrong here: admission asks for one literal word back, and the agent
        # that receives the team instruction OBEYS it -- observed 2026-08-27,
        # where the capture pane finished its turn with an empty composer,
        # `<- 1 agent` in its status bar, and no receipt marker anywhere,
        # because it had gone off to spawn a teammate as instructed. The
        # capture then waited out its whole budget for a reply that the agent
        # was never going to write.
        first_prompt = FIRST_PROMPT.format(marker=marker)
        first_records = _prompt_turn(
            call, first_handle, first_prompt, timeout_ms, marker,
            working_proves=spec.route == "claude")
        first_turn, session_id, reported = _parse_turn(
            spec.route, first_records, marker, first_prompt)
        if spec.route == "claude" and not session_id:
            session_id = _session_from_agent(call, first_handle["name"])
        _stop_agent(call, first_handle)
        first_handle = None
        continuation_handle = _start_visible_agent(
            call, spec, cwd, continuing=True, session_id=session_id)
        continued_with = "-c" if spec.route == "omp" else "--resume"
        continuation_prompt = CONTINUATION_PROMPT
        continuation_records = _prompt_turn(
            call, continuation_handle, continuation_prompt, timeout_ms, marker,
            working_proves=spec.route == "claude")
        continuation, _, _ = _parse_turn(
            spec.route, continuation_records, marker, continuation_prompt)
        if continuation["text"] != first_turn["text"]:
            raise AdmissionError("ROUTE_CONTINUITY_UNPROVEN")
        _stop_agent(call, continuation_handle)
        gone, denial = _agent_gone(call, continuation_handle["name"])
        continuation_handle = None
        if not gone:
            raise AdmissionError(
                "ROUTE_CANCELLATION_UNPROVEN:{0}".format(denial))
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
    # Split downward rather than sideways: successive right-splits divide the
    # width until each agent pane is too narrow to read, while a down-split
    # keeps full width and costs only rows.
    split = call(
        "pane", "split", "--current", "--direction", "down",
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
    # The name carries a per-attempt discriminator. It used to be
    # `admit-<route>-first`/`-cont` verbatim, which is the same name every run
    # of every repository: a capture that ended without closing its panes -- a
    # blocked run, an interrupted bootstrap -- leaves that agent registered, and
    # the next bootstrap is refused `agent_name_taken` before it can do anything
    # at all (D7). Removing the leftover instead is not an option: Herdr reports
    # a healthy agent between turns as `idle`, exactly like an abandoned one, so
    # reclaiming a name would eventually cancel a live run.
    name = _admission_agent_name(spec.route, continuing=continuing)
    argv = _route_argv(spec, continuing=continuing, session_id=session_id)
    start_deadline = time.monotonic() + 60.0
    names_tried = 1
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
            # Branch on Herdr's typed `error.code`, never on its message (§1.2).
            if (exc.code == AGENT_NAME_TAKEN
                    and names_tried < NAME_ATTEMPTS):
                # Step around the leftover with a new name; never take its own.
                name = _admission_agent_name(spec.route, continuing=continuing)
                names_tried += 1
                continue
            if (exc.code != AGENT_PANE_BUSY
                    or time.monotonic() >= start_deadline):
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
    _assert_agent_owns_pane(call, name, pane_id)
    agent = _extract(started, "agent")
    transcript = ""
    if isinstance(agent, dict) and agent.get("transcript_path"):
        transcript = str(agent["transcript_path"])
    if not transcript:
        waited = launcher.wait_for_agent_transcript(
            call, name, spec.timeout_s)
        if waited is not None:
            transcript = str(waited)
    return {"pane_id": pane_id, "name": name, "transcript": transcript}


def _prompt_turn(
        call: Callable[..., dict], handle: Mapping[str, str],
        prompt: str, timeout_ms: str, marker: str,
        *, working_proves: bool = False,
) -> Tuple[dict, ...]:
    timeout_s = max(0.001, int(timeout_ms) / 1000.0)
    # Prove submission from the actor's own transcript, not from the pane.
    #
    # Admission passed no `submission_recorded`, so `submit_agent_prompt` fell
    # back to the pane revision -- and the Claude composer accepts a prompt
    # WITHOUT advancing it. `working_proves` cannot rescue that, because the
    # typed status is consulted only when the meter is UNREADABLE, and a
    # static `1` is perfectly readable. So on 2026-08-27 admission refused
    # `AGENT_PROMPT_UNSUBMITTED:admit-claude-first-...` four times over while
    # the pane sat at status `working`, revision 1, visibly running the turn
    # it had just been told it never received.
    #
    # A submitted prompt appends a record to the session JSONL; a composer
    # that swallowed it appends nothing, and neither does a booting agent.
    # Snapshot the count before offering and require it to rise -- the same
    # rising-count discipline the run path uses, and a typed record rather
    # than a rendering, which is what §1.2 requires of anything a transition
    # keys on.
    #
    # The 2026-08-27 comment that omp's meter "works" (so admission passed
    # `submission_recorded=None` on purpose) is the same hole: paste-repaint
    # advances omp's revision identically to a real submit. OMP therefore
    # takes the rising transcript predicate too. Missing transcript waits,
    # then fails closed -- never the pane revision.
    transcript = str(handle.get("transcript") or "")
    if not transcript:
        waited = launcher.wait_for_agent_transcript(
            call, handle["name"], timeout_s)
        if waited is not None:
            transcript = str(waited)
    if not transcript and not working_proves:
        raise AdmissionError(
            "AGENT_PROMPT_UNOBSERVED:{0} no transcript".format(handle["name"]))
    before = len(_transcript_records(transcript)) if transcript else 0

    def _submitted() -> bool:
        # Either typed fact proves it, and neither alone is enough.
        #
        # A record appended to the session JSONL is the strongest signal, but
        # a transcript that has not appeared yet -- the route herdr reports
        # late -- cannot supply one, and requiring it there refuses a prompt
        # that did land. The agent's own typed `working` status covers exactly
        # that gap.
        #
        # `working` is safe HERE and nowhere else: admission has already
        # waited for the interactive composer before offering, so this is not
        # the boot-time `working` that fooled the run path on 2026-08-27, and
        # a wrong answer still cannot admit a route -- the capture goes on to
        # require the reply marker in the transcript, which no booting agent
        # writes. It lives inside this predicate, never as a consumed()
        # shortcut on the no-transcript path.
        if transcript and len(_transcript_records(transcript)) > before:
            return True
        if not working_proves:
            return False
        # A failed `agent get` propagates on purpose: `submit_agent_prompt`
        # absorbs a raising proof predicate into its typed failure record, so
        # the eventual refusal names the dead probe instead of silently
        # reading it as "not submitted". Swallowing it here would discard
        # exactly the evidence that record exists to keep.
        payload = call("agent", "get", handle["name"])
        agent = _extract(payload, "agent")
        # `agent_status` is herdr's field name; `status` is always None.
        return isinstance(agent, dict) and (
            agent.get("agent_status") or agent.get("status")
        ) == "working"

    def _offer() -> None:
        # Claude's composer can accept a prompt without advancing the pane
        # revision. Its typed working state is equivalent consumption proof
        # only when folded into this predicate, never via the meter.
        #
        # ADMISSION KEEPS ITS BOUNDED WAIT, AND THAT IS NOT THE 2026-08-27
        # DEFECT REPEATING. DO NOT "UNIFY" THIS WITH THE LANE PATH.
        #
        # The lane path stopped convicting at the end of a window because the
        # quantity it was looking at -- how long an agent's turn runs before
        # its transcript records anything -- is unbounded (§7.6: a turn doing
        # real work runs far longer than the 57.7s measured there). No window
        # over an unbounded quantity can be sized correctly, so the refusal at
        # the end of one was a statement about Maestro's clock.
        #
        # This turn is not that quantity. The prompt here is literally "Reply
        # with exactly <marker> and nothing else": one short sentence, one
        # short answer, no tools, no files, no work. Its length is bounded BY
        # CONSTRUCTION, which is what makes a window over it a legitimate
        # measurement rather than an invented one.
        #
        # And the consequence differs. A lane attempt that is offered but
        # unproven is adjudicated downstream by liveness and quiescence, so
        # returning "unproven" costs nothing and loses nothing. Admission has
        # no downstream: its output is a SIGNED ROUTE RECEIPT, and a receipt
        # signed over an unproven route is exactly the thing §1.2 forbids --
        # a durable typed artifact asserting something nobody observed. It
        # must fail closed here or not at all, so `refuse_unproven` keeps its
        # default.
        launcher.submit_agent_prompt(
            call, handle["pane_id"], prompt, handle["name"],
            timeout_s=timeout_s,
            working_proves=working_proves,
            submission_recorded=_submitted)

    try:
        _offer()
    except RuntimeError as exc:
        # A DROPPED paste is not a swallowed Enter, and only one of them may
        # be retried.
        #
        # A Claude pane that is still settling can take `pane send-text` and
        # render nothing: observed 2026-08-27 with the capture pane idle at
        # revision 1, `0 tokens`, and an EMPTY composer -- no text to press
        # Enter on, so every recovery round pressed at nothing and the turn
        # never began.
        #
        # Re-offering is normally forbidden, because a second prompt appends
        # to text still sitting unsent and sends both halves as one garbled
        # turn (the whole reason the recovery loop presses Enter instead of
        # re-prompting). That hazard requires text on screen. When the
        # composer is provably EMPTY there is nothing to append to, so the
        # re-offer is safe -- and it is the only thing that can recover a
        # paste the composer never took.
        if not _composer_is_empty(call, handle["pane_id"], prompt):
            raise AdmissionError(str(exc)) from exc
        try:
            _offer()
        except RuntimeError as retry_exc:
            raise AdmissionError(str(retry_exc)) from retry_exc
    deadline = time.monotonic() + timeout_s
    # Every pane read that failed while polling for the reply marker, by
    # source. The read is a probe retried to the deadline, so each individual
    # failure is survivable — but if the deadline arrives with every read
    # dead, "no reply marker" was never observed, only asserted, and the
    # refusal must say which observations actually failed. Diagnostic only;
    # nothing branches on it (§1.2).
    read_denials: Dict[str, str] = {}
    while True:
        records = list(_transcript_records(handle.get("transcript") or ""))
        if not _reply_marker_present(records, marker, prompt):
            for source in ("recent-unwrapped", "visible", "detection"):
                try:
                    read = call(
                        "pane", "read", handle["pane_id"],
                        "--source", source, "--lines", "120")
                except AdmissionError as exc:
                    read_denials[source] = exc.code or type(exc).__name__
                    continue
                read_denials.pop(source, None)
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
            suffix = ""
            if read_denials:
                suffix = ":pane_unread[{0}]".format(",".join(
                    "{0}={1}".format(source, denial)
                    for source, denial in sorted(read_denials.items())))
            raise AdmissionError("ROUTE_RECEIPT_INCOMPLETE" + suffix)
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


def _admission_agent_name(route: str, *, continuing: bool) -> str:
    """A capture-agent name no other run can already hold.

    The discriminator is random rather than sequential: two installations
    admitting the same route at the same instant must not compute the same
    "next" name.

    Contrast `launcher._agent_name`, which is deliberately left deterministic.
    Its correlation token carries a per-run `uuid4` `run_id`, so a node agent
    name is already collision-free by construction and a discriminator there
    would only churn a name that post-mortems key on. Route admission has no
    such run identity -- its name was derived from the route alone -- which is
    why the collision lands here and only here.
    """
    return "admit-{}-{}-{}".format(
        route, "cont" if continuing else "first", uuid.uuid4().hex[:8])


def _assert_agent_owns_pane(
        call: Callable[..., dict], name: str, pane_id: str) -> None:
    """Refuse to prompt a name that resolves anywhere but our own pane.

    `herdr agent prompt <TARGET> <TEXT>` types the text wherever Herdr resolves
    `TARGET`. A name is a durable Herdr-side handle, so a name that once
    belonged to some other agent -- a leftover record, a recycled pane -- sends
    the admission prompt into whatever shell now sits there, which is how
    `Reply with exactly MAESTRO_OMP_RECEIPT_OK...` ended up on the operator's
    own command line instead of in the capture pane.

    The pane we just split is the only correct destination, and `agent get`
    reports the pane each agent occupies, so the two are compared before any
    text is submitted. A pane Herdr will not report is not proof of anything, so
    it is refused too: this runs before the prompt, where refusing is free.
    """
    try:
        payload = call("agent", "get", name)
    except AdmissionError as exc:
        raise AdmissionError(
            "ROUTE_AGENT_TARGET_UNPROVEN:{}".format(name)) from exc
    agent = _extract(payload, "agent")
    bound = (str(agent.get("pane_id") or "")
             if isinstance(agent, dict) else "")
    if bound != pane_id:
        raise AdmissionError(
            "ROUTE_AGENT_TARGET_MISMATCH:{}:{}!={}".format(
                name, bound or "?", pane_id))


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


#: Herdr's typed `error.code` values that positively assert the asked-about
#: pane no longer exists. Any other failure of the absence probe is a missing
#: observation — a timeout, a dead socket, a mid-restart server — and a
#: missing observation must never be read as proof of absence.
PANE_ABSENT_CODES = ("pane_not_found", "workspace_not_found")

#: Herdr's typed refusal when it holds no record of the requested agent.
AGENT_ABSENT_CODES = ("agent_not_found",)


def _stop_agent(call: Callable[..., dict], handle: Mapping[str, str]) -> None:
    """Close the pane and prove it is gone.

    `herdr pane close` answers with the generic success envelope
    (`{"result": {"type": "ok"}}`); the API schema carries no `closed` field for
    it. Absence of the pane afterwards is the only real evidence, so ask for it.
    """
    try:
        call("pane", "close", handle["pane_id"])
    except AdmissionError:
        # Deliberate: close's reply is not the evidence. The absence probe
        # below is, and it fails closed.
        pass
    gone, denial = _pane_gone(call, handle["pane_id"])
    if not gone:
        raise AdmissionError(
            "ROUTE_CANCELLATION_UNPROVEN:{0}:{1}".format(
                handle["pane_id"], denial))


def _pane_gone(call: Callable[..., dict], pane_id: str) -> Tuple[bool, str]:
    """(gone, denial): absence proven, or the observation that failed to prove it.

    Only Herdr's own typed absence codes prove the pane gone. This used to
    read *any* failed `pane get` as absence, so a transport failure while
    herdr was unreachable counted as a proven cancellation — the failed
    observation was indistinguishable from the negative evidence it was
    supposed to produce. `denial` is diagnostic only: it names why absence is
    unproven; nothing branches on it (§1.2).
    """
    try:
        payload = call("pane", "get", pane_id)
    except AdmissionError as exc:
        if exc.code in PANE_ABSENT_CODES:
            return True, ""
        return False, "pane_unreadable:{0}".format(
            exc.code or type(exc).__name__)
    if isinstance(_extract(payload, "pane"), dict):
        return False, "pane_still_present"
    return True, ""


def _agent_gone(call: Callable[..., dict], name: str) -> Tuple[bool, str]:
    """(gone, denial) — same contract and same reasoning as `_pane_gone`."""
    try:
        payload = call("agent", "get", name)
    except AdmissionError as exc:
        if exc.code in AGENT_ABSENT_CODES:
            return True, ""
        return False, "agent_unreadable:{0}".format(
            exc.code or type(exc).__name__)
    agent = _extract(payload, "agent")
    if isinstance(agent, dict):
        return False, "agent_still_present"
    return True, ""


def _best_effort_close(call: Callable[..., dict], handle: Mapping[str, str]) -> None:
    """Deliberate swallow: cleanup on an already-refusing path.

    Every caller is inside a `finally` or after a raise is committed; the
    refusal already in flight is the evidence, and a close failure here must
    not replace it. Callers that need cancellation *proven* use `_stop_agent`.
    """
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
        refusal = (result.stderr or result.stdout).strip()
        raise AdmissionError("LAUNCH_REFUSED:{}".format(refusal[-400:]),
                             herdr_error_code(refusal))
    try:
        payload = json.loads(result.stdout.strip() or "{}")
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



def _composer_is_empty(
        call: Callable[..., dict], pane_id: str, prompt: str) -> bool:
    """True only when the pane demonstrably does NOT hold the offered text.

    Read defensively and answer False on any doubt: a wrong True re-sends a
    prompt on top of one already composed, which is the garbled-turn failure
    re-offering is otherwise banned to avoid. An unreadable pane is doubt.
    """
    probe = prompt.strip().splitlines()[0][:40].strip() if prompt.strip() else ""
    if not probe:
        return False
    try:
        payload = call("pane", "read", pane_id, "--source", "visible")
    except Exception:
        return False
    text = ""
    result = payload.get("result") if isinstance(payload, dict) else None
    if isinstance(result, dict):
        text = str(result.get("text") or "")
    if not text:
        return False
    return probe not in text


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
    """The agent's typed session value, or `""` when none is observable.

    Deliberate swallow: this is a fallback probe (the transcript parse is the
    primary source), the empty string is its typed "nothing observable"
    answer, and the claude path fails closed behind it — a receipt without a
    session refuses `ROUTE_RECEIPT_INCOMPLETE` rather than admitting.
    """
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
