"""Readmit work from an attempt that died between its last write and its commit.

An agent that finishes its files and dies before `commit_measured_delta`
leaves those bytes outside the evidence chain. Copying them into a later
attempt is dishonest either way: before that attempt's baseline they vanish
from the delta; after it they are attributed to work no attempt of a2
performed. This module admits the bytes as a truthful `commit_measured_delta`
for the attempt that produced them.

The measurement is taken in that attempt's own bracket — recorded base
commit to surviving worktree — and the commit is parented on that attempt's
own ref. Anything that cannot prove both is refused, not approximated.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Tuple


from . import lifecycle as lc
from . import receipt_crypto as rc
from . import scheduler_types as st
from . import watchdog as wd
from . import worktree as wt


RECORD_KIND = "maestro-salvage.v1"


class SalvageRefused(RuntimeError):
    """A salvage the operator asked for that this verb will not perform.

    `outcome` is the refusal vocabulary. `fields` are the typed facts a
    caller can branch on without reading `detail`. `reason` is never one
    of those facts: it is an audit field and §1.2 forbids deciding a
    lifecycle transition from free text.
    """

    def __init__(self, outcome: str, detail: str, **fields: Any) -> None:
        super().__init__(detail)
        self.outcome = outcome
        self.detail = detail
        self.fields = fields


@dataclass(frozen=True)
class SalvageResult:
    """What a successful salvage published."""

    run_id: str
    node_id: str
    attempt_no: int
    base_sha: str
    output_sha: str
    record_path: Path
    signature_path: Path
    files: Tuple[Dict[str, Any], ...]
    #: Declared outputs on disk that git will not commit, so `output_sha` does
    #: not carry them. `None` means the question could not be asked — this
    #: attempt's baseline predates the recorded ignored-at-base map — and is
    #: not the same as the empty tuple, which is a measured "none" (#67).
    uncommittable_outputs: Optional[Tuple[str, ...]] = None


def _require_stated(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SalvageRefused(
            "SALVAGE_INVOKER_REQUIRED",
            f"{field} must be a non-empty statement; salvage is a deliberate, "
            "attributable operator act")
    return value


def _require_signing_seed(seed: object) -> bytes:
    if not isinstance(seed, (bytes, bytearray)) or len(seed) != rc.SEED_SIZE:
        raise SalvageRefused(
            "SALVAGE_SIGNING_REQUIRED",
            "salvage needs a 32-byte Ed25519 signing seed; an unsigned "
            "admission is indistinguishable from tampering")
    return bytes(seed)


def _file_records(attempt: wt.AttemptWorktree,
                  measured: wt.InventoryDelta) -> Tuple[Dict[str, Any], ...]:
    records = []
    for rel in measured.added:
        full = attempt.path / rel
        data = full.read_bytes()
        records.append({
            "path": rel,
            "action": "added",
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        })
    for rel in measured.changed:
        full = attempt.path / rel
        data = full.read_bytes()
        records.append({
            "path": rel,
            "action": "changed",
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        })
    for rel in measured.removed:
        records.append({"path": rel, "action": "removed"})
    return tuple(records)


def _record_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _write_signed_record(record_dir: Path, run_id: str, node_id: str,
                         attempt_no: int, payload: Mapping[str, Any],
                         seed: bytes) -> Tuple[Path, Path]:
    record_dir = Path(record_dir)
    record_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{run_id}.{node_id}.a{attempt_no}"
    path = record_dir / f"{stem}.json"
    signature_path = record_dir / f"{stem}.json.sig"
    if path.exists() or signature_path.exists():
        raise SalvageRefused(
            "SALVAGE_OUTPUT_EXISTS",
            f"a salvage record already exists at {path}")
    data = _record_bytes(payload)
    signature = rc.sign(seed, data)
    path.write_bytes(data)
    signature_path.write_text(signature.hex() + "\n", encoding="ascii")
    return path, signature_path


def load_record(path: Path, public_key: bytes) -> Dict[str, Any]:
    """Verify and parse a salvage record. Used by tests and audit readers."""
    data = Path(path).read_bytes()
    signature_path = Path(str(path) + ".sig")
    signature = bytes.fromhex(signature_path.read_text(encoding="ascii").strip())
    if not rc.verify(public_key, data, signature):
        raise SalvageRefused(
            "SALVAGE_RECORD_INVALID",
            f"{path} does not verify under the supplied key")
    return json.loads(data.decode("utf-8"))


def _attempt_ref_sha(repo: Path, run_id: str, node_id: str,
                     attempt_no: int) -> Optional[str]:
    ref = "refs/heads/{}".format(wt.branch_name(run_id, node_id, attempt_no))
    resolved = wt._git(
        Path(repo), "rev-parse", "--verify", "--quiet", "{}^{{commit}}".format(ref),
        check=False)
    if resolved.returncode == 0:
        return resolved.stdout.strip()
    if resolved.returncode == 1:
        return None
    raise SalvageRefused(
        "SALVAGE_BRACKET_UNPROVEN",
        f"git rev-parse of {ref} exited {resolved.returncode}: "
        f"{resolved.stderr.strip()}")



def _refuse_if_already_committed(repo: Path, attempt_row: st.AttemptRecord) -> None:
    if attempt_row.extra.get("salvage_output_sha"):
        raise SalvageRefused(
            "SALVAGE_OUTPUT_EXISTS",
            f"{attempt_row.run_id}/{attempt_row.node_id}#{attempt_row.attempt_no} "
            f"already has output commit {attempt_row.extra['salvage_output_sha']}")
    current = _attempt_ref_sha(
        repo, attempt_row.run_id, attempt_row.node_id, attempt_row.attempt_no)
    if current is None or current == attempt_row.base_sha:
        return
    raise SalvageRefused(
        "SALVAGE_BASE_MOVED",
        f"{attempt_row.run_id}/{attempt_row.node_id}#{attempt_row.attempt_no} "
        f"ref is {current}, not recorded base {attempt_row.base_sha} — "
        "the attempt is no longer at its own bracket")



def _recorded_baseline(store: lc.LifecycleStore, run_id: str, node_id: str,
                       attempt_no: int) -> Dict[str, Tuple[str, str]]:
    """The attempt's own recorded baseline, or a refusal — never a rebuild.

    The measurement bracket's before-side is the *provisioned* tree, which
    holds untracked paths no commit contains. Rebuilding it from the base
    commit after the attempt died reports each of those as a path the attempt
    added; one of them covered by the node's declared outputs is committed,
    measured as the attempt's delta, and signed for. Absence is therefore a
    refusal, not a licence to approximate (§1.1 item 4).
    """
    try:
        return store.attempt_baseline(run_id, node_id, attempt_no)
    except lc.BaselineUnrecorded as exc:
        raise SalvageRefused(
            "SALVAGE_BASELINE_UNRECORDED",
            f"{run_id}/{node_id}#{attempt_no} has no recorded measurement "
            "baseline; its before-side cannot be rebuilt from the base commit "
            "without attributing provisioned untracked content to the attempt",
            run_id=run_id, node_id=node_id, attempt_no=attempt_no) from exc
    except lc.BaselineCorrupt as exc:
        raise SalvageRefused(
            "SALVAGE_BASELINE_CORRUPT", str(exc),
            run_id=run_id, node_id=node_id, attempt_no=attempt_no) from exc


def _recorded_ignored_at_base(store: lc.LifecycleStore, run_id: str,
                              node_id: str,
                              attempt_no: int) -> Optional[Dict[str, str]]:
    """The attempt's ignored-at-base map, or `None` for "nobody looked".

    Deliberately not a refusal, and deliberately not `{}`. Salvage exists so
    an operator with stranded work has a verb instead of hand-rolled git, and
    refusing every attempt whose baseline predates `ignored_json` would take
    that verb away from exactly the runs most likely to need it. `{}` is not
    available either: it claims the tree held no ignored files when the
    bracket opened, and measuring against that claim reports a whole
    provisioned dependency tree as content the attempt wrote.

    So the third answer is the honest one — the map is unknown, the
    uncommittable-output question cannot be asked of this attempt, and the
    signed record says which of the two it was (#67).

    A *corrupt* map is still a refusal, because that is a ledger that
    disagrees with itself rather than one that predates a column.
    """
    try:
        return store.attempt_ignored_at_base(run_id, node_id, attempt_no)
    except lc.BaselineCorrupt as exc:
        raise SalvageRefused(
            "SALVAGE_BASELINE_CORRUPT", str(exc),
            run_id=run_id, node_id=node_id, attempt_no=attempt_no) from exc


def _refuse_if_live(store: lc.LifecycleStore, attempt: st.AttemptRecord) -> None:
    if attempt.state is not st.NodeState.RUNNING:
        return
    store._require_scheduler_dead(attempt.run_id)
    if attempt.pid is not None:
        if wd.process_is_alive(attempt.pid):
            raise SalvageRefused(
                "SALVAGE_ATTEMPT_LIVE",
                f"{attempt.run_id}/{attempt.node_id}#{attempt.attempt_no} "
                f"still has live pid {attempt.pid}")
        return
    if attempt.launched_at is not None:
        raise SalvageRefused(
            "SALVAGE_ATTEMPT_LIVE",
            f"{attempt.run_id}/{attempt.node_id}#{attempt.attempt_no} "
            "launched but recorded no pid; refusing rather than guessing "
            "the process is dead")


def salvage_attempt(
        store: lc.LifecycleStore, *,
        run_id: str,
        node_id: str,
        attempt_no: int,
        repo: Path,
        worktrees_root: Path,
        scratch_root: Path,
        invoked_by: str,
        reason: str,
        signing_seed: bytes,
        record_dir: Path,
        clock: Callable[[], float] = time.time,
) -> SalvageResult:
    """Measure a1 in a1's bracket and commit on a1's own ref.

    Does not seed a later attempt. Does not mark the node VERIFIED or
    MERGED: the post-node gate never ran, and skip remains the operator
    path that supplies a SHA to the merge protocol.
    """
    invoked_by = _require_stated(invoked_by, "invoked_by")
    reason = _require_stated(reason, "reason")
    seed = _require_signing_seed(signing_seed)
    repo = Path(repo)
    try:
        store._require_escape_legal(run_id)
    except lc.EscapeRefused as exc:
        raise SalvageRefused("ESCAPE_REFUSED", str(exc)) from exc

    attempt_row = store.get_attempt(run_id, node_id, attempt_no)
    try:
        _refuse_if_live(store, attempt_row)
    except lc.EscapeRefused as exc:
        raise SalvageRefused("ESCAPE_REFUSED", str(exc)) from exc

    declared = store.node_outputs(run_id, node_id)
    _refuse_if_already_committed(repo, attempt_row)
    # Read before the worktree is reopened, so an attempt whose before-side
    # was never recorded costs nothing and leaves no commit and no record.
    recorded_baseline = _recorded_baseline(store, run_id, node_id, attempt_no)
    recorded_ignored = _recorded_ignored_at_base(
        store, run_id, node_id, attempt_no)


    worktree_path = (
        Path(worktrees_root) / wt.worktree_dirname(run_id, node_id, attempt_no))
    if not worktree_path.is_dir():
        raise SalvageRefused(
            "SALVAGE_WORKTREE_ABSENT",
            f"attempt worktree {worktree_path} is gone")

    try:
        attempt = wt.reopen_attempt_worktree(
            repo, run_id, node_id, attempt_no, attempt_row.base_sha,
            Path(worktrees_root), Path(scratch_root))
    except wt.WorktreeError as exc:
        raise SalvageRefused("SALVAGE_BRACKET_UNPROVEN", str(exc)) from exc

    try:
        head = wt._out(attempt.path, "rev-parse", "HEAD")
    except wt.WorktreeError as exc:
        raise SalvageRefused("SALVAGE_BRACKET_UNPROVEN", str(exc)) from exc
    if head != attempt.base:
        raise SalvageRefused(
            "SALVAGE_BASE_MOVED",
            f"HEAD is {head}, not the attempt's recorded base {attempt.base}")

    # `reopen_attempt_worktree` leaves `baseline` unset, because the base
    # commit cannot rebuild it: `git ls-tree` is tracked paths only and §8.3's
    # baseline deliberately includes provisioned untracked content. Supplying
    # the recorded one is what opens the bracket for this measurement, and it
    # is the only thing that may.
    attempt.baseline = recorded_baseline
    after = wt.inventory(attempt.path)
    measured = wt.delta(attempt.baseline, after)
    if measured.is_empty:
        raise SalvageRefused(
            "SALVAGE_EMPTY_DELTA",
            f"{run_id}/{node_id}#{attempt_no} has no measured delta against "
            "its recorded base")

    permission = wt.permission_check(attempt, measured, declared)
    if not permission.passes:
        raise SalvageRefused(
            "SALVAGE_PERMISSION_DENIED",
            f"{run_id}/{node_id}#{attempt_no} fails §8.3's permission check",
            conjunct1=list(permission.conjunct1_violations),
            conjunct2=list(permission.conjunct2_violations))

    # A declared output git will not commit. The scheduler blocks this at
    # attempt settle (`DECLARED_OUTPUT_UNCOMMITTABLE`) and a run refuses it at
    # start, and salvage took neither path: the same node could have its
    # stranded work salvaged into a commit that does not carry the output,
    # under a signed record asserting a digest over what *was* committed.
    #
    # Recorded rather than refused, on this verb's own purpose. A refusal here
    # leaves the operator with stranded work and no verb, which is the state
    # salvage exists to end -- and the work is real either way; what was
    # missing is a statement of what the commit could not hold. So the commit
    # is written, and the signed record carries the gap beside it. The
    # operator learns it from the receipt rather than from a silence.
    uncommittable: Tuple[str, ...] = ()
    if recorded_ignored is not None:
        try:
            uncommittable = wt.existing_ignored_outputs(
                attempt.path, declared, after, recorded_ignored)
        except wt.WorktreeError as exc:
            raise SalvageRefused(
                "SALVAGE_BRACKET_UNPROVEN", str(exc)) from exc

    files = _file_records(attempt, measured)
    try:
        output_sha = wt.commit_measured_delta(
            attempt, measured, after,
            f"{node_id} attempt {attempt_no}")
    except wt.HeadMoved as exc:
        raise SalvageRefused("SALVAGE_BASE_MOVED", str(exc)) from exc
    except wt.CompareAndSwapRefused as exc:
        raise SalvageRefused("SALVAGE_BASE_MOVED", str(exc)) from exc
    except wt.WorktreeError as exc:
        raise SalvageRefused("SALVAGE_BRACKET_UNPROVEN", str(exc)) from exc

    payload = {
        "kind": RECORD_KIND,
        "run_id": run_id,
        "node_id": node_id,
        "attempt_no": attempt_no,
        "base_sha": attempt.base,
        "baseline_digest": lc.baseline_digest(
            lc.encode_baseline(recorded_baseline)),
        "output_sha": output_sha,
        "invoked_by": invoked_by,
        "reason": reason,
        "created_at_epoch": clock(),
        "files": list(files),
        # Declared outputs that exist on disk and that git will not commit, so
        # the `output_sha` above does not carry them. Three states, and they
        # are not interchangeable: a list names the paths the commit is
        # missing; `[]` says the question was asked and the answer was none;
        # `null` says it could not be asked, because this attempt's baseline
        # predates the ignored-at-base map and no before-side exists to
        # measure against. A reader that collapses `null` into `[]` turns
        # "unknown" into "clean", which is the reading this field was added to
        # make impossible (#67).
        "uncommittable_outputs": (
            None if recorded_ignored is None else list(uncommittable)),
    }
    record_path, signature_path = _write_signed_record(
        Path(record_dir), run_id, node_id, attempt_no, payload, seed)
    verified = load_record(record_path, rc.seed_to_public_key(seed))
    if verified.get("output_sha") != output_sha:
        raise SalvageRefused(
            "SALVAGE_RECORD_INVALID",
            f"{record_path} does not name the commit just written")

    store.record_salvage(
        run_id, node_id, attempt_no,
        extra={
            "salvage_output_sha": output_sha,
            "salvage_record": str(record_path),
            "salvage_invoked_by": invoked_by,
        })
    return SalvageResult(
        run_id=run_id, node_id=node_id, attempt_no=attempt_no,
        base_sha=attempt.base, output_sha=output_sha,
        record_path=record_path, signature_path=signature_path,
        files=files,
        uncommittable_outputs=(
            None if recorded_ignored is None else uncommittable))
