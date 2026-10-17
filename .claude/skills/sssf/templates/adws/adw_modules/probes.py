"""Reviewer-authored probes: parse, check, run, discharge, receipt, pin.

A code reviewer that finds a real defect the accepted suite does not cover can
submit a probe: a test file, written in its own checkout, that cites the
contract and fails on the candidate. Prose findings are un-executable, so a builder satisfied the visible suite and
every sentence of prose, and a lane plateau-parked on a green suite while the
reviewer named a distinct defect each round.

This module owns the parts that are decisions about bytes, not about
workflow:

- `parse_envelope` validates the reviewer's submission shape before any file
  is read, and `read_submissions` reads the files bounded and without
  following links.
- `check_submission` is the mechanical validity half: runner class, verbatim
  contract citation, parseability, imports limited to the declared interface.
  What it cannot decide (does the expectation follow from the cited
  requirement, is the failure for the intended reason) is a judgment the
  acceptor makes, and the receipt records it.
- `classify_admission` and `discharge` read ONE execution (`rr.ProbeExecution`)
  -- exit code and complete per-case report together. Discharge never consults
  a count: it is the exit code, a whole report, the frozen id set, every case
  PASSED.
- `make_receipt` / `validate_receipts` / `parse_acceptance` carry the
  acceptor's typed verdict. A probe is admitted or retired only against a
  receipt whose digest binds probe, source, candidate and requirement.
- `pin_sources` writes the active probes' sources as a commit under an
  immutable ref named for that commit, so a late worker can never move a
  winner's evidence.

Nothing here writes `lane_state`, merges a probe into integration, or reads
reviewer prose as a transition key.
"""

from __future__ import annotations

import ast
import dataclasses
import errno
import os
import re
import stat
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import bound_surface as bs
from . import review_contract as rc
from . import runner_resolution as rr
from . import scheduler_types as st
from . import tests_chain as tc
from .git_helper import BoundGit, GitError, zero_oid

PROBE_FAILED = st.PROBE_FAILED

MAX_PROBES_PER_ROUND = 3
MAX_PROBE_BYTES = 65536
MAX_PROBE_CASES = 20

# Rejections: the reviewer's submission is wrong, and is told so once.
PROBE_ENVELOPE_MALFORMED = "PROBE_ENVELOPE_MALFORMED"
PROBE_COUNT_EXCEEDED = "PROBE_COUNT_EXCEEDED"
PROBE_PATH_REFUSED = "PROBE_PATH_REFUSED"
PROBE_FILE_MISSING = "PROBE_FILE_MISSING"
PROBE_FILE_UNSAFE = "PROBE_FILE_UNSAFE"
PROBE_TOO_LARGE = "PROBE_TOO_LARGE"
PROBE_NOT_UTF8 = "PROBE_NOT_UTF8"
PROBE_NOT_PARSEABLE = "PROBE_NOT_PARSEABLE"
PROBE_UNCITED = "PROBE_UNCITED"
PROBE_RUNNER_MISMATCH = "PROBE_RUNNER_MISMATCH"
PROBE_IMPORT_UNDECLARED = "PROBE_IMPORT_UNDECLARED"
PROBE_TOO_MANY_CASES = "PROBE_TOO_MANY_CASES"
PROBE_INSTRUMENTATION_ERROR = "PROBE_INSTRUMENTATION_ERROR"
PROBE_NOT_ACCEPTED = "PROBE_NOT_ACCEPTED"
PROBE_UNCOLLECTED = "PROBE_UNCOLLECTED"

# Non-admissions: nothing the reviewer did wrong, and never a block on its
# ordinary verdict.
PROBE_ACCEPTOR_UNAVAILABLE = "PROBE_ACCEPTOR_UNAVAILABLE"
PROBE_ACCEPTOR_NOT_INDEPENDENT = "PROBE_ACCEPTOR_NOT_INDEPENDENT"
PROBE_ACCEPTANCE_MALFORMED = "PROBE_ACCEPTANCE_MALFORMED"
PROBE_RUN_INCOMPLETE = "PROBE_RUN_INCOMPLETE"
PROBE_RUNNER_UNSUPPORTED = "PROBE_RUNNER_UNSUPPORTED"

# `classify_admission` outcomes that are not rejection codes.
ADMISSION_FAILING = "FAILING"
ADMISSION_PASSED_ON_FIRST_RUN = "PASSED_ON_FIRST_RUN"

STATUS_DISCHARGED = "DISCHARGED"
STATUS_OPEN = "OPEN"

MODE_ACCEPT = "accept"
MODE_CHALLENGE = "challenge"
_MODES = (MODE_ACCEPT, MODE_CHALLENGE)
_VERDICTS = ("PASS", "REVISE")

ACCEPTOR_ROLES = ("test-reviewer", "tester")
AUTHOR_ROLE = "code-reviewer"


@dataclasses.dataclass(frozen=True)
class ProbeEntry:
    """One submission as listed in the envelope, before its file is read."""

    path: str
    requirement: str
    intent: str


@dataclasses.dataclass(frozen=True)
class ProbeSubmission:
    path: str
    source: str
    requirement: str
    intent: str


@dataclasses.dataclass(frozen=True)
class Challenge:
    probe_id: str
    reason: str


@dataclasses.dataclass(frozen=True)
class Rejection:
    path: str
    code: str
    detail: str = ""


class ProbeReceiptError(rc.ReviewContractError):
    """A record, retirement or receipt does not bind to what it claims."""


class ProbeAcceptanceMalformed(rc.ReviewContractError):
    """The acceptor's answer is not a valid response to what was asked."""

    code = PROBE_ACCEPTANCE_MALFORMED


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------


def _components(path: str) -> tuple[str, ...]:
    return tuple(path.split("/"))


def _overlaps(left: str, right: str) -> bool:
    a, b = _components(left), _components(right)
    size = min(len(a), len(b))
    return a[:size] == b[:size]


def _reserved_overlap(path: str, reserved: Sequence[str]) -> str | None:
    for other in reserved:
        try:
            normal = rc.normalize_repo_path(other)
        except rc.ReviewContractError:
            continue
        if _overlaps(path, normal):
            return normal
    return None


def _nonempty_str(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def parse_envelope(
    raw: object,
    *,
    reserved: Sequence[str] = (),
) -> tuple[tuple[ProbeEntry, ...], tuple[Rejection, ...], tuple[Challenge, ...]]:
    """Validate the `probes` and `challenges` of a reviewer envelope.

    Runs before any file is read. `reserved` is every path a probe may not
    touch: declared outputs, accepted suite paths and the paths of probes
    already accumulated. Overlap is by path component, so a probe may not sit
    under, over or on a reserved path, nor on another submission in its batch.
    """
    if raw is None:
        return (), (), ()
    if not isinstance(raw, Mapping):
        return (), (Rejection("", PROBE_ENVELOPE_MALFORMED, "envelope is not a mapping"),), ()
    rejections: list[Rejection] = []
    entries: list[ProbeEntry] = []
    challenges: list[Challenge] = []

    probes = raw.get("probes")
    if probes is not None and not isinstance(probes, list):
        rejections.append(Rejection("", PROBE_ENVELOPE_MALFORMED, "probes is not a list"))
        probes = None
    for index, item in enumerate(probes or ()):
        label = item.get("path") if isinstance(item, Mapping) else None
        label = label if isinstance(label, str) else "probes[{0}]".format(index)
        if index >= MAX_PROBES_PER_ROUND:
            rejections.append(
                Rejection(
                    label,
                    PROBE_COUNT_EXCEEDED,
                    "at most {0} probes per round".format(MAX_PROBES_PER_ROUND),
                )
            )
            continue
        if not isinstance(item, Mapping) or set(item) != {"path", "requirement", "intent"}:
            rejections.append(
                Rejection(
                    label,
                    PROBE_ENVELOPE_MALFORMED,
                    "entry must be exactly path, requirement and intent",
                )
            )
            continue
        if not all(_nonempty_str(item[key]) for key in ("path", "requirement", "intent")):
            rejections.append(
                Rejection(label, PROBE_ENVELOPE_MALFORMED, "entry fields must be non-empty strings")
            )
            continue
        try:
            path = rc.normalize_repo_path(item["path"])
        except rc.ReviewContractError as extra:
            rejections.append(Rejection(label, PROBE_PATH_REFUSED, str(extra)))
            continue
        if any(part.casefold() == ".git" for part in _components(path)):
            rejections.append(Rejection(path, PROBE_PATH_REFUSED, "path is inside .git"))
            continue
        clash = _reserved_overlap(path, reserved)
        if clash is not None:
            rejections.append(
                Rejection(path, PROBE_PATH_REFUSED, "path overlaps reserved path {0}".format(clash))
            )
            continue
        peer = next((entry.path for entry in entries if _overlaps(path, entry.path)), None)
        if peer is not None:
            rejections.append(
                Rejection(path, PROBE_PATH_REFUSED, "path overlaps submission {0}".format(peer))
            )
            continue
        entries.append(ProbeEntry(path, item["requirement"], item["intent"]))

    found = raw.get("challenges")
    if found is not None and not isinstance(found, list):
        rejections.append(Rejection("", PROBE_ENVELOPE_MALFORMED, "challenges is not a list"))
        found = None
    seen: set[str] = set()
    for index, item in enumerate(found or ()):
        label = "challenges[{0}]".format(index)
        if index >= MAX_PROBES_PER_ROUND:
            rejections.append(Rejection(label, PROBE_COUNT_EXCEEDED, "too many challenges"))
            continue
        if (
            not isinstance(item, Mapping)
            or set(item) != {"probe_id", "reason"}
            or not _nonempty_str(item["probe_id"])
            or not _nonempty_str(item["reason"])
        ):
            rejections.append(
                Rejection(label, PROBE_ENVELOPE_MALFORMED, "challenge must be probe_id and reason")
            )
            continue
        if item["probe_id"] in seen:
            rejections.append(Rejection(label, PROBE_ENVELOPE_MALFORMED, "duplicate challenge"))
            continue
        seen.add(item["probe_id"])
        challenges.append(Challenge(item["probe_id"], item["reason"]))
    return tuple(entries), tuple(rejections), tuple(challenges)


def _open_under(root_fd: int, path: str) -> int:
    """Open a regular file under `root_fd` without following any link.

    Raises `_Missing` or `_Unsafe`; any other `OSError` is infrastructure and
    propagates untouched.
    """
    parts = _components(path)
    fd = os.dup(root_fd)
    try:
        for index, part in enumerate(parts):
            last = index == len(parts) - 1
            try:
                info = os.stat(part, dir_fd=fd, follow_symlinks=False)
            except OSError as extra:
                if extra.errno in (errno.ENOENT, errno.ENOTDIR):
                    raise _Missing(path) from extra
                raise
            if stat.S_ISLNK(info.st_mode):
                raise _Unsafe("{0}: symlink".format(path))
            if last:
                if stat.S_ISDIR(info.st_mode):
                    raise _Missing(path)
                if not stat.S_ISREG(info.st_mode):
                    raise _Unsafe("{0}: not a regular file".format(path))
                try:
                    opened = os.open(part, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
                except OSError as extra:
                    if extra.errno == errno.ELOOP:
                        raise _Unsafe("{0}: symlink".format(path)) from extra
                    raise
                os.close(fd)
                return opened
            if not stat.S_ISDIR(info.st_mode):
                raise _Missing(path)
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except OSError as extra:
                if extra.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise _Unsafe("{0}: symlink".format(path)) from extra
                raise
            os.close(fd)
            fd = child
        raise _Missing(path)
    except BaseException:
        os.close(fd)
        raise


class _Missing(Exception):
    pass


class _Unsafe(Exception):
    pass


def read_submissions(
    root: Path,
    entries: Sequence[ProbeEntry],
    *,
    limit: int = MAX_PROBE_BYTES,
) -> tuple[tuple[ProbeSubmission, ...], tuple[Rejection, ...]]:
    """Read each listed file under `root`: regular, unlinked, bounded, UTF-8.

    A bad entry is a typed rejection and the rest of the batch still reads.
    An `OSError` that is not a missing or unsafe file is infrastructure and
    propagates; it is not reported as the reviewer's fault.
    """
    submissions: list[ProbeSubmission] = []
    rejections: list[Rejection] = []
    root_fd = os.open(str(root), os.O_RDONLY | os.O_DIRECTORY)
    try:
        for entry in entries:
            try:
                fd = _open_under(root_fd, entry.path)
            except _Missing:
                rejections.append(Rejection(entry.path, PROBE_FILE_MISSING))
                continue
            except _Unsafe as extra:
                rejections.append(Rejection(entry.path, PROBE_FILE_UNSAFE, str(extra)))
                continue
            try:
                chunks: list[bytes] = []
                size = 0
                while size <= limit:
                    chunk = os.read(fd, min(65536, limit + 1 - size))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    size += len(chunk)
            finally:
                os.close(fd)
            if size > limit:
                rejections.append(
                    Rejection(entry.path, PROBE_TOO_LARGE, "over {0} bytes".format(limit))
                )
                continue
            try:
                text = b"".join(chunks).decode("utf-8")
            except UnicodeDecodeError:
                rejections.append(Rejection(entry.path, PROBE_NOT_UTF8))
                continue
            submissions.append(
                ProbeSubmission(entry.path, text, entry.requirement, entry.intent)
            )
    finally:
        os.close(root_fd)
    return tuple(submissions), tuple(rejections)


# ---------------------------------------------------------------------------
# Mechanical validity
# ---------------------------------------------------------------------------

_SOURCE_EXTENSIONS = (".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
_JS_SPECIFIER = re.compile(r"""(?:\bfrom|\bimport|\brequire)\s*\(?\s*['"]([^'"\n]+)['"]""")


def _module_key(value: str) -> str:
    text = value.strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    for extension in _SOURCE_EXTENSIONS:
        if text.endswith(extension):
            text = text[: -len(extension)]
            break
    text = text.replace("/", ".").strip(".")
    if text.endswith(".__init__"):
        text = text[: -len(".__init__")]
    return text


def _declared_interface(
    interface: Sequence[Mapping[str, Any]],
) -> dict[str, set[str]]:
    declared: dict[str, set[str]] = {}
    for entry in interface or ():
        if not isinstance(entry, Mapping):
            continue
        module = entry.get("module")
        if not isinstance(module, str) or not module.strip():
            continue
        names = declared.setdefault(_module_key(module), set())
        name = entry.get("name")
        if isinstance(name, str) and name:
            names.add(name)
            names.add(name.split(".")[0])
    return declared


def _declared_match(
    key: str, declared: Mapping[str, set[str]]
) -> set[str] | None:
    for module, names in declared.items():
        if key == module or key.endswith("." + module):
            return names
    return None


def _in_tree(tree: Path, key: str, suffixes: Sequence[str]) -> bool:
    base = Path(tree)
    relative = key.replace(".", "/")
    for prefix in ("", "src/"):
        for suffix in suffixes:
            if (base / (prefix + relative + suffix)).is_file():
                return True
    return False


def _python_import_findings(
    source: str, tree: Path, declared: Mapping[str, set[str]]
) -> list[str]:
    problems: list[str] = []
    parsed = ast.parse(source)
    for node in ast.walk(parsed):
        if isinstance(node, ast.ImportFrom) and node.level:
            problems.append("relative import is not a declared interface module")
    surface = bs.derive_bound_surface({"probe.py": source})
    for module in surface["modules"]:
        key = _module_key(module["specifier"])
        if not _in_tree(tree, key, (".py", "/__init__.py")):
            continue
        names = _declared_match(key, declared)
        if names is None:
            problems.append("{0} is not a declared interface module".format(module["specifier"]))
            continue
        for symbol in module["symbols"]:
            if symbol not in names:
                problems.append(
                    "{0}.{1} is not a declared interface name".format(module["specifier"], symbol)
                )
    return problems


def _javascript_import_findings(
    path: str, source: str, tree: Path, declared: Mapping[str, set[str]]
) -> list[str]:
    problems: list[str] = []
    here = Path(path).parent
    for specifier in _JS_SPECIFIER.findall(source):
        if not specifier.startswith("."):
            continue
        joined = os.path.normpath(str(here / specifier))
        if joined.startswith(".."):
            problems.append("{0} leaves the repository".format(specifier))
            continue
        key = _module_key(joined)
        resolved = (Path(tree) / joined).is_file() or any(
            (Path(tree) / (joined + extension)).is_file()
            or (Path(tree) / joined / ("index" + extension)).is_file()
            for extension in _SOURCE_EXTENSIONS
        )
        if not resolved:
            continue
        if _declared_match(key, declared) is None:
            problems.append("{0} is not a declared interface module".format(specifier))
    return problems


def check_submission(
    submission: ProbeSubmission,
    *,
    contract_text: str,
    interface: Sequence[Mapping[str, Any]],
    runner: str,
    tree: Path,
) -> tuple[Rejection, ...]:
    """Every mechanical validity check, as typed rejections.

    `runner` is the runner the accepted suite derives; a probe must name the
    same one. `interface` is the lane's declared interface; a probe may import
    only declared modules and names from the candidate tree. Call-through into
    undeclared internals and whether the failure is for the intended reason are
    judgments left to the acceptor.
    """
    out: list[Rejection] = []
    path = submission.path
    if tc._file_runner(path) != runner:
        out.append(
            Rejection(path, PROBE_RUNNER_MISMATCH, "a {0} suite cannot take this file".format(runner))
        )
        return tuple(out)
    quote = st.normalize_requirement_quote(submission.requirement)
    contract = st.normalize_requirement_quote(contract_text)
    if len(quote) < st.MIN_CONTRACT_QUOTE or quote not in contract:
        out.append(
            Rejection(path, PROBE_UNCITED, "requirement is not a verbatim quote of the contract")
        )
    declared = _declared_interface(interface)
    if path.endswith(".py"):
        try:
            ast.parse(submission.source)
        except (SyntaxError, ValueError, RecursionError) as extra:
            out.append(Rejection(path, PROBE_NOT_PARSEABLE, str(extra)[:200]))
            return tuple(out)
        problems = _python_import_findings(submission.source, tree, declared)
    else:
        problems = _javascript_import_findings(path, submission.source, tree, declared)
    for problem in problems:
        out.append(Rejection(path, PROBE_IMPORT_UNDECLARED, problem))
    return tuple(out)


def write_probe_tree(tree: Path, sources: Mapping[str, str | bytes]) -> tuple[str, ...]:
    """Write probe sources into a materialized review tree.

    Refuses a link anywhere on the path, and refuses a path the candidate
    already occupies with different bytes: a probe is added beside the
    candidate, never over it.
    """
    root = Path(tree).resolve()
    written: list[str] = []
    for path, body in sorted(sources.items()):
        relative = rc.normalize_repo_path(path)
        data = body.encode("utf-8") if isinstance(body, str) else bytes(body)
        target = root / relative
        probe = root
        for part in _components(relative)[:-1]:
            probe = probe / part
            if probe.is_symlink():
                raise rc.ReviewContractError("PROBE_PATH_UNSAFE:{0}".format(relative))
        if target.is_symlink():
            raise rc.ReviewContractError("PROBE_PATH_UNSAFE:{0}".format(relative))
        if target.exists():
            if not target.is_file() or target.read_bytes() != data:
                raise rc.ReviewContractError("PROBE_PATH_OCCUPIED:{0}".format(relative))
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        written.append(relative)
    return tuple(written)


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def run_probe(
    resolved: rr.ResolvedRunner,
    tree: Path,
    path: str,
    scratch_root: Path,
    timeout_s: float = rr.EXECUTE_TIMEOUT_S,
) -> rr.ProbeExecution:
    """One execution of one probe file: exit code and whole report together."""
    return rr.execute_probe(resolved, tree, path, scratch_root, timeout_s)


def frozen_case_ids(execution: rr.ProbeExecution) -> tuple[str, ...]:
    return tuple(sorted(execution.cases))


def classify_admission(execution: rr.ProbeExecution) -> str:
    """What one execution of a NEW probe means for admission.

    `FAILING` goes to acceptance. `PASSED_ON_FIRST_RUN` is not admitted. An
    `ERROR` state is the probe's own instrumentation failing, attributed to the
    reviewer. Every other combination -- a missing, partial or ambiguous
    report, a non-zero exit with only passing states, a zero exit beside a
    failing state -- is `PROBE_RUN_INCOMPLETE`: no conclusion is drawn.
    """
    if execution.report_error == rr.PROBE_REPORT_UNSUPPORTED:
        return PROBE_RUNNER_UNSUPPORTED
    if execution.report_error or not execution.cases:
        return PROBE_RUN_INCOMPLETE
    if len(execution.cases) > MAX_PROBE_CASES:
        return PROBE_TOO_MANY_CASES
    states = set(execution.cases.values())
    if rr.PROBE_ERROR in states:
        return PROBE_INSTRUMENTATION_ERROR
    if execution.returncode == 0 and states == {rr.PROBE_PASSED}:
        return ADMISSION_PASSED_ON_FIRST_RUN
    if execution.returncode != 0 and rr.PROBE_FAILED_STATE in states:
        return ADMISSION_FAILING
    return PROBE_RUN_INCOMPLETE


def discharge(execution: rr.ProbeExecution, frozen_ids: Sequence[str]) -> bool:
    """True only when this one execution proves the frozen cases all pass.

    The exit code is zero, the report parsed whole, the reduced id set EQUALS
    the frozen set (neither missing nor extra), and every state is PASSED.
    Counts are never consulted; a skip never discharges.
    """
    if execution.returncode != 0 or execution.report_error:
        return False
    if not frozen_ids or set(execution.cases) != set(frozen_ids):
        return False
    return all(state == rr.PROBE_PASSED for state in execution.cases.values())


# ---------------------------------------------------------------------------
# Records, receipts, acceptance
# ---------------------------------------------------------------------------


def source_digest(source: str | bytes) -> str:
    data = source.encode("utf-8") if isinstance(source, str) else bytes(source)
    return st.digest_bytes(data)


def requirement_digest(requirement: str) -> str:
    return st.digest_canonical({"requirement": st.normalize_requirement_quote(requirement)})


def failure_digest(case_ids: Sequence[str], lines: Sequence[str]) -> str:
    return st.digest_canonical({"cases": sorted(case_ids), "lines": list(lines)})


def probe_id(path: str, source_sha256: str, requirement: str) -> str:
    return "p-" + st.digest_canonical(
        {"path": path, "requirement": requirement, "source_sha256": source_sha256}
    )[:12]


def make_record(
    *,
    path: str,
    source: str | bytes,
    blob_oid: str,
    requirement: str,
    intent: str,
    case_ids: Sequence[str],
    admitted_in: str,
    acceptance_receipt_id: str,
) -> dict[str, Any]:
    """The cumulative immutable record of one admitted probe."""
    ids = sorted(case_ids)
    if not ids or len(ids) > MAX_PROBE_CASES or len(set(ids)) != len(ids):
        raise rc.ReviewContractError(PROBE_TOO_MANY_CASES)
    digest = source_digest(source)
    return {
        "acceptance_receipt_id": acceptance_receipt_id,
        "admitted_in": admitted_in,
        "blob_oid": blob_oid,
        "case_ids": ids,
        "intent": intent,
        "path": path,
        "probe_id": probe_id(path, digest, requirement),
        "requirement": requirement,
        "source_sha256": digest,
    }


_RECEIPT_KEYS = (
    "acceptor_role",
    "acceptor_vendor",
    "author_vendor",
    "candidate_sha",
    "failure_sha256",
    "input_digest",
    "mode",
    "probe_id",
    "requirement_sha256",
    "source_sha256",
    "verdict",
)


def make_receipt(
    *,
    mode: str,
    probe_id: str,
    source_sha256: str,
    candidate_sha: str,
    input_digest: str,
    requirement_sha256: str,
    failure_sha256: str,
    verdict: str,
    acceptor_role: str,
    author_vendor: str,
    acceptor_vendor: str,
    reason: str = "",
) -> dict[str, Any]:
    """A canonical acceptance receipt. `reason` is evidence, outside the digest."""
    if mode not in _MODES:
        raise rc.ReviewContractError("receipt mode must be accept or challenge")
    if verdict not in _VERDICTS:
        raise rc.ReviewContractError("receipt verdict must be PASS or REVISE")
    body = {
        "acceptor_role": acceptor_role,
        "acceptor_vendor": acceptor_vendor,
        "author_vendor": author_vendor,
        "candidate_sha": candidate_sha,
        "failure_sha256": failure_sha256,
        "input_digest": input_digest,
        "mode": mode,
        "probe_id": probe_id,
        "requirement_sha256": requirement_sha256,
        "source_sha256": source_sha256,
        "verdict": verdict,
    }
    return {**body, "receipt_id": st.digest_canonical(body), "reason": str(reason)}


def _receipt_body(receipt: Mapping[str, Any]) -> dict[str, Any]:
    return {key: receipt.get(key) for key in _RECEIPT_KEYS}


def validate_receipts(
    records: Sequence[Mapping[str, Any]],
    retired: Sequence[Mapping[str, Any]],
    receipts: Sequence[Mapping[str, Any]],
) -> None:
    """Every admission and retirement must reference a receipt that binds it.

    Raises `ProbeReceiptError`. Run when a review is built and again whenever a
    prior artifact's probes are read back, so a forged or mismatched receipt is
    refused at the point it would be trusted.
    """
    by_id: dict[str, Mapping[str, Any]] = {}
    for receipt in receipts:
        rid = receipt.get("receipt_id")
        if st.digest_canonical(_receipt_body(receipt)) != rid:
            raise ProbeReceiptError("receipt digest does not match its body")
        if rid in by_id:
            raise ProbeReceiptError("duplicate receipt id")
        by_id[rid] = receipt
    known: dict[str, Mapping[str, Any]] = {}
    for record in records:
        pid = record.get("probe_id")
        if pid in known:
            raise ProbeReceiptError("duplicate probe record {0}".format(pid))
        known[pid] = record
        if pid != probe_id(
            str(record.get("path")),
            str(record.get("source_sha256")),
            str(record.get("requirement")),
        ):
            raise ProbeReceiptError("probe id does not match its record")
        receipt = by_id.get(record.get("acceptance_receipt_id"))
        if receipt is None:
            raise ProbeReceiptError("probe {0} has no acceptance receipt".format(pid))
        if (
            receipt["mode"] != MODE_ACCEPT
            or receipt["verdict"] != "PASS"
            or receipt["probe_id"] != pid
            or receipt["source_sha256"] != record.get("source_sha256")
            or receipt["requirement_sha256"] != requirement_digest(str(record.get("requirement")))
        ):
            raise ProbeReceiptError("probe {0} is not bound to a passing accept receipt".format(pid))
    seen: set[str] = set()
    for entry in retired:
        pid = entry.get("probe_id")
        if pid not in known or pid in seen:
            raise ProbeReceiptError("retirement names no single admitted probe")
        seen.add(pid)
        receipt = by_id.get(entry.get("challenge_receipt_id"))
        if (
            receipt is None
            or receipt["mode"] != MODE_CHALLENGE
            or receipt["verdict"] != "REVISE"
            or receipt["probe_id"] != pid
            or receipt["source_sha256"] != known[pid].get("source_sha256")
        ):
            raise ProbeReceiptError(
                "retirement of {0} is not bound to a REVISE challenge receipt".format(pid)
            )


def active_records(
    records: Sequence[Mapping[str, Any]], retired: Sequence[Mapping[str, Any]]
) -> tuple[Mapping[str, Any], ...]:
    gone = {entry.get("probe_id") for entry in retired}
    return tuple(record for record in records if record.get("probe_id") not in gone)


def parse_acceptance(
    raw: object,
    requested_ids: Sequence[str],
    mode: str,
) -> dict[str, tuple[str, str]]:
    """The acceptor's answer as `{probe_id: (verdict, reason)}`, or a refusal.

    Strict, and run before anything is mutated: one entry per requested id, no
    unknown or duplicate id, and a top-level verdict that is PASS exactly when
    every item is. Item verdicts are authoritative; the aggregate only has to
    agree. Any violation raises one `ProbeAcceptanceMalformed` for the batch.
    """
    if mode not in _MODES:
        raise ProbeAcceptanceMalformed("mode must be accept or challenge")
    if not isinstance(raw, Mapping):
        raise ProbeAcceptanceMalformed("acceptance is not a mapping")
    items = raw.get("probe_verdicts")
    if not isinstance(items, list):
        raise ProbeAcceptanceMalformed("probe_verdicts is not a list")
    wanted = list(requested_ids)
    answers: dict[str, tuple[str, str]] = {}
    for item in items:
        if not isinstance(item, Mapping) or set(item) != {"probe_id", "verdict", "reason"}:
            raise ProbeAcceptanceMalformed("verdict entry must be probe_id, verdict and reason")
        pid, verdict, reason = item["probe_id"], item["verdict"], item["reason"]
        if not isinstance(pid, str) or pid not in wanted:
            raise ProbeAcceptanceMalformed("unknown probe id")
        if pid in answers:
            raise ProbeAcceptanceMalformed("duplicate probe id")
        if verdict not in _VERDICTS:
            raise ProbeAcceptanceMalformed("verdict must be PASS or REVISE")
        if not isinstance(reason, str):
            raise ProbeAcceptanceMalformed("reason must be a string")
        answers[pid] = (verdict, reason)
    if set(answers) != set(wanted):
        raise ProbeAcceptanceMalformed("every requested probe needs exactly one verdict")
    aggregate = raw.get("verdict")
    expected = "PASS" if all(v == "PASS" for v, _ in answers.values()) else "REVISE"
    if aggregate != expected:
        raise ProbeAcceptanceMalformed("aggregate verdict contradicts the item verdicts")
    return answers


def _vendor(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip().casefold()
    return None


def select_acceptor(
    role_vendors: Mapping[str, object] | None,
) -> tuple[str, str, str] | Rejection:
    """The acceptor role and both vendors, or `PROBE_ACCEPTOR_NOT_INDEPENDENT`.

    Independence is operator-declared: the code reviewer's vendor and the
    acceptor's must both be declared and differ. A different route, profile or
    model is not independence, and an undeclared vendor is not a guess. Tries
    `test-reviewer`, then `tester`.
    """
    vendors = role_vendors or {}
    author = _vendor(vendors.get(AUTHOR_ROLE))
    if author is None:
        return Rejection("", PROBE_ACCEPTOR_NOT_INDEPENDENT, "code-reviewer declares no vendor")
    for role in ACCEPTOR_ROLES:
        acceptor = _vendor(vendors.get(role))
        if acceptor is not None and acceptor != author:
            return role, author, acceptor
    return Rejection(
        "",
        PROBE_ACCEPTOR_NOT_INDEPENDENT,
        "no acceptor role declares a vendor different from the code-reviewer's",
    )


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------

_OBSERVED_CHARS = 600


def probe_failed_finding(
    record: Mapping[str, Any], lines: Sequence[str]
) -> dict[str, str]:
    """The harness's own finding for a probe that is still failing.

    The cited requirement is the probe's verbatim quote of the contract, so the
    finding is bound to the same text a reviewer finding must quote. `code`
    marks it harness-synthesized; a reviewer envelope carrying one is refused.
    """
    evidence = "; ".join(str(line) for line in lines[:3]) or "the probe's cases fail"
    return {
        "implementation_area": str(record["path"]),
        "observed_behavior": "probe failed: " + evidence[:_OBSERVED_CHARS],
        "required_behavior": str(record["intent"]),
        "violated_requirement": str(record["requirement"]),
        "axis": st.FINDING_AXIS_SPEC,
        "code": PROBE_FAILED,
    }


# ---------------------------------------------------------------------------
# Durable storage
# ---------------------------------------------------------------------------


def probe_ref(run_id: str, lane_id: str, commit: str) -> str:
    return "refs/maestro/probes/{0}/{1}/{2}".format(run_id, lane_id, commit)


def pin_sources(
    git: BoundGit,
    run_id: str,
    lane_id: str,
    sources: Mapping[str, str | bytes],
) -> tuple[str, str]:
    """Pin the active probes' sources under an immutable output-addressed ref.

    One parentless commit whose tree is exactly `sources`, with fixed author
    and date, so identical sources give the same commit on every call. The ref
    name carries the commit sha and is created once: if it exists it must
    already resolve to this commit, otherwise the pin is refused. Nothing here
    ever moves or deletes a ref, so a late worker with divergent sources writes
    its own ref and cannot touch the winner's evidence.
    """
    files = {
        rc.normalize_repo_path(path): (
            body.encode("utf-8") if isinstance(body, str) else bytes(body)
        )
        for path, body in sources.items()
    }
    empty = git.hash_object(b"", object_type="tree")
    tree = git.overlay_tree(empty, files)
    commit = git.commit_tree(tree, (), b"maestro probes\n", epoch_seconds=0)
    ref = probe_ref(run_id, lane_id, commit)
    existing = git.read_ref(ref)
    if existing is None:
        try:
            git.update_ref(ref, commit, zero_oid(git.object_format()))
        except GitError:
            existing = git.read_ref(ref)
            if existing is None:
                raise
    if existing is not None and existing != commit:
        raise rc.ReviewContractError(
            "PROBE_REF_MISMATCH:{0} resolves to {1}".format(ref, existing)
        )
    return commit, ref


def read_pinned_sources(git: BoundGit, commit: str) -> dict[str, bytes]:
    """The sources pinned in `commit`, read from the object database alone."""
    listing = git.run("ls-tree", "-r", "-z", commit).stdout
    sources: dict[str, bytes] = {}
    for row in listing.split(b"\0"):
        if not row:
            continue
        meta, _, name = row.partition(b"\t")
        oid = meta.split()[2].decode("ascii")
        sources[name.decode("utf-8")] = git.cat_file("blob", oid)
    return sources
