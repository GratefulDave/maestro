"""Run the sealed suite against a builder's working tree. Not workflow authority.

The builder's checkout holds every test file in the repository except the one
that decides its lane. So every round is a blind guess: it writes code, submits,
and learns minutes later that it was wrong from a count plus another model's
prose about one redacted failure line. That prose is not constrained to agree
with the assertion, and has been its exact inverse.

This module hands the builder the same bytes the factory already mails it --
the five counts and the redacted failure lines -- in seconds instead of
minutes, as often as it likes, before it submits. Nothing new is revealed:
`probe_tree` runs the identical measurement `code_review.measure_candidate`
runs, and prints through the identical redaction. It reuses those functions
rather than reimplementing them, so the two cannot drift into disagreeing about
what the builder may see.

Two properties this file must keep:

* **It never mutates its subject.** The checkout is read; the measurement runs
  in a scratch tree outside it, which is removed in a `finally`. A harness
  command that edits what it measures is the `vitest list --json <paths>`
  incident in `.claude/rules/lane_diagnosis.md`.
* **It never prints a path under the runtime state root.** The vault path and
  the scratch path are redaction tokens, and every refusal that reaches stderr
  is sanitized against them. That sanitizing is only as good as its ordering:
  the redaction set is seeded from the registry entry before anything under
  the state root is opened, because the first thing opened there is the vault
  and `ensure_vault` raises carrying its own path.
* **It agrees with the factory or it says nothing.** Every verdict the review
  path can reach, this path reaches too, from the same functions. A green
  reading over a candidate the factory will refuse is worse than no probe: it
  is the wrong oracle the builder already has.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping, Sequence

from . import code_review as cr
from . import hidden_vault as hv
from . import private_review as pr
from . import scheduler as sch
from . import scheduler_types as st
from . import tests_chain as tc
from .lifecycle import ArtifactStore
from .reporting_registry import registry_path as default_registry_path
from .runtime_state import LEDGER_FILENAME

#: The counts the factory reports as `public_result_summary`, in the order the
#: builder reads them. Exactly these five, no more: a sixth would be a new
#: disclosure, not a formatting choice.
COUNT_KEYS = ("executed", "passed", "failed", "errored", "skipped")

WITHHELD_LINE = "failure lines withheld: they did not survive the redaction check"

#: Fixed text, and fixed for a reason: the colliding path is a sealed test
#: path, so naming it would hand the builder the one byte the whole private
#: boundary exists to withhold. It is told what to do, not which file did it.
COLLISION_LINE = (
    "refused: your working tree holds a path the sealed suite owns; "
    "the factory will refuse this candidate"
)

_REDACTED_PATH = "[redacted]"


class ProbeRefused(RuntimeError):
    """The probe itself could not run. Never a statement about the candidate."""


@dataclasses.dataclass(frozen=True)
class ProbeResult:
    """What the sealed suite says about one working tree.

    `withheld` is not a soft failure mode: it means the redaction backstop
    refused the failure lines, and the counts ship alone. The remedy is to fix
    the extractor, never to widen an allow-list until a line prints.

    `collision` means no suite ran, because the tree holds a path the sealed
    suite owns and the factory will refuse the candidate for it. Counts are
    zero and mean nothing; a count here would be a green reading of a
    candidate that cannot pass, which is the wrong-oracle defect this module
    exists to remove.
    """

    counts: Mapping[str, int]
    failure_lines: tuple[str, ...]
    withheld: bool
    collision: bool = False


@dataclasses.dataclass(frozen=True)
class LaneProbe:
    """Everything `probe_tree` needs, read off the ledger for one lane."""

    run_id: str
    lane_id: str
    state_root: Path
    repo: Path
    runtime_root: Path
    vault: Path
    private_files: Mapping[str, str]
    sealed_ref: str
    sealed_digest: str
    gate: Any
    provision_argv: tuple[str, ...]
    provision_timeout_s: float | None


# -- working-tree overlay ---------------------------------------------------


def _is_inside(child: Path, parent: Path) -> bool:
    try:
        Path(child).resolve().relative_to(Path(parent).resolve())
    except ValueError:
        return False
    return True


def worktree_paths(checkout: Path) -> tuple[str, ...]:
    """Every path `git status --porcelain -z` reports, renames included.

    `-z` emits one NUL-terminated `XY <path>` record per entry, and for a
    rename or copy a *second* NUL-terminated field carrying the original path.
    Both halves are returned: the overlay rule below copies whichever exists
    and unlinks whichever does not, so a rename falls out of it rather than
    needing a case of its own.

    `git stash create` is not an alternative here -- it drops untracked files,
    which is precisely the builder's new source file.
    """
    raw = hv._git(checkout, "status", "--porcelain", "-z", text=False).stdout
    fields = [item.decode("utf-8", "surrogateescape") for item in raw.split(b"\0")]
    while fields and fields[-1] == "":
        fields.pop()
    out: list[str] = []
    index = 0
    while index < len(fields):
        entry = fields[index]
        index += 1
        # "XY " plus at least one character of path.
        if len(entry) < 4:
            continue
        status_x, status_y, path = entry[0], entry[1], entry[3:]
        out.append(path)
        if status_x in "RC" or status_y in "RC":
            if index < len(fields):
                out.append(fields[index])
                index += 1
    return tuple(out)


def _copy_into(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    _remove(dest)
    if src.is_symlink():
        dest.symlink_to(os.readlink(src))
        return
    if src.is_dir():
        return
    shutil.copy2(src, dest)


def _remove(target: Path) -> None:
    if target.is_symlink() or target.is_file():
        target.unlink()
    elif target.is_dir():
        shutil.rmtree(target)


def _overlay_one(checkout: Path, tree: Path, rel: str) -> None:
    rel = rel.strip()
    if not rel:
        return
    parts = [part for part in rel.split("/") if part not in ("", ".")]
    if not parts or ".." in parts or parts[0] == ".git":
        return
    directory = rel.endswith("/")
    src = checkout.joinpath(*parts)
    dest = tree.joinpath(*parts)
    if not _is_inside(dest.parent, tree) and dest.parent != tree:
        raise ProbeRefused("working-tree path escapes the scratch tree")
    if directory:
        # `--porcelain` without `-uall` reports an untracked directory as one
        # entry with a trailing slash. Walk it so its files land individually.
        if src.is_dir() and not src.is_symlink():
            for child in sorted(src.rglob("*")):
                if child.is_dir() and not child.is_symlink():
                    continue
                _copy_into(child, tree / child.relative_to(checkout))
        return
    if os.path.lexists(src):
        _copy_into(src, dest)
    else:
        _remove(dest)


def overlay_working_tree(checkout: Path, tree: Path) -> None:
    """Make `tree` read as the checkout's working tree, not as its HEAD.

    `measure_candidate` materializes an immutable candidate sha. The probe must
    not require a commit -- requiring one defeats the purpose -- so HEAD is
    materialized and every path git reports as changed is overlaid on top.
    """
    checkout = Path(checkout)
    tree = Path(tree)
    for rel in worktree_paths(checkout):
        _overlay_one(checkout, tree, rel)


# -- measurement ------------------------------------------------------------


def _counts_from_run(run: Mapping[str, object]) -> dict[str, int]:
    counts = run["counts"]
    assert isinstance(counts, Mapping)
    return {
        "executed": int(run["executed"]),  # type: ignore[arg-type]
        "passed": int(counts["passed"]),
        "failed": int(counts["failed"]),
        "errored": int(counts["errored"]),
        "skipped": int(counts["skipped"]),
    }


def _sorted_tokens(tokens: Sequence[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for token in sorted((item for item in tokens if item), key=len, reverse=True):
        if token not in seen:
            seen.add(token)
            out.append(token)
    return tuple(out)


def _result_from_run(
    run: Mapping[str, object],
    *,
    vault: Path,
    files: Mapping[str, str],
    sealed_digest: str,
    sealed_ref: str,
    run_id: str,
    lane_id: str,
    secret_paths: Sequence[Path],
) -> ProbeResult:
    """The five counts and the failure lines, redacted the way a review is.

    Token collection is `review_builder_output`'s, argument for argument, so
    the probe cannot admit a byte the factory would withhold. The scratch tree
    is added to the token set on top of that: it is a path under nobody's
    control but this process's, and it must never reach the builder.
    """
    counts = _counts_from_run(run)
    private_bytes = st.canonical_bytes(
        {
            "counts": run["counts"],
            "executed": run["executed"],
            "output": run["output"],
            "returncode": run["returncode"],
        }
    )
    results_digest = st.digest_bytes(private_bytes)
    vault_refs: list[str] = [ref for ref in (sealed_ref,) if ref]
    if run_id and lane_id:
        vault_refs.append(hv.private_results_ref(run_id, lane_id, results_digest))
    sources = {
        path: hv.cat_blob(vault, blob).decode("utf-8") for path, blob in files.items()
    }
    tokens = _sorted_tokens(
        pr.collect_private_tokens(
            files=sources,
            vault_path=vault,
            vault_refs=tuple(vault_refs),
            blob_ids=tuple(files.values()),
        )
        + tuple(str(Path(path)) for path in secret_paths)
    )
    surface_names = cr._bound_surface_names(sources)
    failure_lines = cr.redacted_failure_lines(
        str(run["output"]), tokens, allow=surface_names
    )
    payload = {
        "public_result_summary": counts,
        "redacted_failures": list(failure_lines),
    }
    allowed = (results_digest, sealed_digest) + surface_names
    try:
        pr.refuse_private_leak(payload, tokens, allow=allowed)
    except pr.PrivateLeakError:
        return ProbeResult(counts=counts, failure_lines=(), withheld=True)
    return ProbeResult(counts=counts, failure_lines=failure_lines, withheld=False)


def _collides(dest: Path, files: Mapping[str, str]) -> bool:
    """Whether the tree already occupies a path the sealed suite owns.

    The predicate is `code_review`'s own, called rather than restated, so the
    probe and the review can never disagree about what counts as a collision.
    It signals by raising and its message names the sealed path, so the
    exception is converted to a bool here and never allowed to reach the
    builder. A plain `IsolationError` -- a sealed path escaping the tree -- is
    a different and worse condition and is left to propagate.
    """
    try:
        cr._refuse_candidate_private_collisions(Path(dest), files)
    except pr.PrivatePathCollisionError:
        return True
    return False


def _collision_result() -> ProbeResult:
    return ProbeResult(
        counts={key: 0 for key in COUNT_KEYS},
        failure_lines=(),
        withheld=False,
        collision=True,
    )


def probe_tree(
    checkout: Path,
    *,
    repo: Path,
    vault: Path,
    private_files: Mapping[str, str],
    gate: Any,
    provision_argv: Sequence[str],
    provision_timeout_s: float | None,
    runtime_root: Path,
    scratch_parent: Path | None = None,
    run_id: str = "",
    lane_id: str = "",
    sealed_ref: str = "",
    sealed_digest: str = "",
) -> ProbeResult:
    """Measure one working tree against the sealed suite. Pure measurement.

    No registry, no ledger, no lane state: everything it needs is an argument,
    so the whole path is exercisable without a run. `private_files` is the
    mapping `tests_chain.sealed_private_files` returns -- repository path to
    vault blob id -- and the sources are read from the vault here, once,
    because both the token set and the bound-surface allow-list are derived
    from them.
    """
    checkout = Path(checkout).resolve()
    files = dict(private_files)
    if not files:
        raise ProbeRefused("the sealed bundle has no private tests")
    head = hv.rev_parse(checkout, "HEAD")
    parent = None
    if scratch_parent is not None:
        parent = Path(scratch_parent)
        parent.mkdir(parents=True, exist_ok=True)
    scratch = Path(
        tempfile.mkdtemp(prefix="maestro-probe-", dir=str(parent) if parent else None)
    ).resolve()
    try:
        if _is_inside(scratch, checkout):
            raise ProbeRefused("the scratch tree would sit inside the checkout")
        dest = scratch / "tree"
        hv.materialize_commit(repo, head, dest)
        overlay_working_tree(checkout, dest)
        # `measure_candidate` refuses a candidate that occupies a sealed path,
        # so a probe that let the blob quietly win would report green over a
        # candidate the factory is going to refuse -- a wrong oracle, which is
        # the whole defect this module removes. Same predicate, imported, so
        # the two verdicts cannot drift.
        #
        # Checked twice against one function. Before provisioning because the
        # collision is a property of the builder's own tree and it should not
        # cost minutes to hear about; again after, because that is where the
        # factory checks, and matching the factory is the point. Provisioning
        # only adds files, so an early hit is always a late hit too.
        if _collides(dest, files):
            return _collision_result()
        cr.provision_tree(dest, provision_argv, provision_timeout_s)
        if _collides(dest, files):
            return _collision_result()
        hv.copy_blobs_to_tree(vault, dest, files)
        run, _refusal = cr._run_sealed_suite(
            dest, files, gate=gate, runtime_root=Path(runtime_root)
        )
        return _result_from_run(
            run,
            vault=Path(vault),
            files=files,
            sealed_digest=sealed_digest,
            sealed_ref=sealed_ref,
            run_id=run_id,
            lane_id=lane_id,
            secret_paths=(scratch, dest),
        )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def render(result: ProbeResult) -> str:
    """The exact bytes the builder reads. Five counts, then lines or nothing.

    A collision prints one line and no counts: there are no counts, and
    printing zeros beside a refusal invites reading them as a measurement.
    """
    if result.collision:
        return COLLISION_LINE
    lines = [
        "{0}: {1}".format(key, int(result.counts.get(key, 0))) for key in COUNT_KEYS
    ]
    if result.withheld:
        lines.append(WITHHELD_LINE)
    else:
        lines.extend(result.failure_lines)
    return "\n".join(lines)


# -- lane resolution --------------------------------------------------------


def _installations(registry: Path) -> tuple[Mapping[str, Any], ...]:
    try:
        payload = json.loads(Path(registry).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ProbeRefused("the installation registry is unreadable") from exc
    entries = payload.get("installations") if isinstance(payload, Mapping) else None
    if not isinstance(entries, list):
        raise ProbeRefused("the installation registry lists no installation")
    return tuple(item for item in entries if isinstance(item, Mapping))


def _ledger_knows_run(database: Path, run_id: str) -> bool:
    if not Path(database).is_file():
        return False
    try:
        conn = sqlite3.connect("file:{0}?mode=ro".format(database), uri=True)
    except sqlite3.Error:
        return False
    try:
        row = conn.execute(
            "SELECT 1 FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
    except sqlite3.Error:
        return False
    finally:
        conn.close()
    return row is not None


def installation_for_run(
    run_id: str, *, registry_path: Path | None = None
) -> Mapping[str, Any]:
    """The registered installation whose ledger holds this run.

    The registry is observational -- it records where a run was started, and
    says nothing about stage -- so it is used for exactly that: finding the
    ledger. Every fact after this comes out of the ledger itself.
    """
    registry = Path(registry_path) if registry_path else default_registry_path()
    for entry in _installations(registry):
        database = entry.get("database")
        if isinstance(database, str) and _ledger_knows_run(Path(database), run_id):
            return entry
    raise ProbeRefused("no registered installation holds that run")


def _plan_gate(store: ArtifactStore, run_id: str, lane_id: str) -> Any:
    """The lane's gate, off the plan revision this run is bound to.

    The gate is not in the ledger: `dag_lanes` carries the projection, not the
    lane spec. It is read from the plan artifact the run recorded, and the
    plan's digest is checked against the run's before a single field is used,
    so an edited plan file refuses rather than silently measuring against a
    gate this run never admitted.
    """
    import maestro  # local: the deployment config loader lives in the CLI

    from . import git_publication as gitpub

    run = store._run(run_id)
    revision = run["plan_revision"]
    row = store.conn.execute(
        "SELECT plan_artifact_ref FROM plan_revisions "
        "WHERE run_id=? AND plan_revision=?",
        (run_id, revision),
    ).fetchone()
    if row is None:
        raise ProbeRefused("the run records no plan revision")
    ref = str(row["plan_artifact_ref"])
    try:
        compiled = maestro._compile_plan(Path(ref), revision=int(revision), ref=ref)
    except Exception as exc:
        raise ProbeRefused("the run's plan artifact is unreadable") from exc
    if compiled.plan_digest != run["plan_digest"]:
        raise ProbeRefused("the plan artifact no longer matches the run's plan")
    specs = gitpub.lane_specs_from_plan(compiled)
    return sch._lane_gate(SimpleNamespace(lane_specs=specs), lane_id)


def _provisioning() -> tuple[tuple[str, ...], float | None]:
    """The deployment's provisioning command, read the way the scheduler reads it.

    `_resolved_provision_argv` takes it off an actor's launcher rather than
    re-reading the config, so the probe presents the same shape: a launcher
    carrying the deployment's declared `provision_argv` and timeout.
    """
    import maestro  # local, as above

    layout = maestro._load_deployment_config(Path(maestro.__file__))
    actor = SimpleNamespace(
        launcher=SimpleNamespace(
            provision_argv=tuple(layout.get("provision_argv") or ()),
            provision_timeout_s=layout.get("provision_timeout_s"),
        )
    )
    return (
        sch._resolved_provision_argv(actor, None),
        sch._resolved_provision_timeout(actor),
    )


def secret_paths(entry: Mapping[str, Any], run_id: str) -> tuple[str, ...]:
    """Every path derived from one installation, for redacting an error line.

    Seeded from the registry entry alone, so it is known BEFORE anything under
    the state root is opened. That ordering is the whole point: `ensure_vault`
    raises `VaultError` carrying the vault path, and an empty redaction set at
    that moment prints the runtime state root to the builder's stderr.

    Both the raw and the resolved spelling of the root are included, because
    `hidden_vault` resolves before composing and `/tmp` is `/private/tmp` here;
    the derived children are included so the longest match wins over the bare
    root, leaving a redacted line that is readable rather than a path stump.
    """
    out: list[str] = []
    state = entry.get("state")
    if isinstance(state, str) and state:
        root = Path(state)
        roots = {root, Path(os.path.realpath(root))}
        for candidate in roots:
            out.append(str(candidate))
            out.append(str(candidate / LEDGER_FILENAME))
            try:
                out.append(str(hv.vault_path(candidate, run_id)))
            except (hv.VaultError, ValueError, OSError):
                pass
    database = entry.get("database")
    if isinstance(database, str) and database:
        out.append(database)
    return tuple(dict.fromkeys(item for item in out if item))


def resolve_lane(
    run_id: str,
    lane_id: str,
    *,
    registry_path: Path | None = None,
    installation: Mapping[str, Any] | None = None,
) -> LaneProbe:
    """Read one lane's sealed bundle and gate out of the live ledger.

    `lane_id` is the lane the builder is working -- the *build* lane. The
    sealed bundle it is bound by belongs to the tests lane it needs, and
    `ArtifactStore._sealed_bundle` is what already walks that edge for the
    scheduler, plan revision and all. Asking it, rather than searching for a
    bundle, is what keeps the probe measuring the same suite the review will.

    `installation` is the already-resolved registry entry. `main` resolves it
    first so it can seed its redaction set from the state root before this
    function touches anything underneath it, and passes the same entry back in
    rather than re-reading the registry -- a second read could return a
    different installation than the one the redaction set was built from.
    """
    entry = installation or installation_for_run(
        run_id, registry_path=registry_path
    )
    state_root = Path(str(entry["state"]))
    repo = Path(str(entry["repository"]))
    store = ArtifactStore(state_root / LEDGER_FILENAME)
    try:
        row = store._sealed_bundle(run_id, lane_id)
        if row is None:
            raise ProbeRefused("that lane has no sealed test bundle yet")
        record = store.get_lane_artifact(row["artifact_id"])
        sealed = sch._record_as_lane_artifact(
            record,
            SimpleNamespace(
                spec_digest=row["spec_digest"],
                lane_projection_digest=row["lane_projection_digest"],
            ),
        )
        gate = _plan_gate(store, run_id, record.lane_id)
    finally:
        store.close()
    provision_argv, provision_timeout_s = _provisioning()
    vault = hv.ensure_vault(state_root, run_id)
    files = tc.sealed_private_files(vault, sealed)
    return LaneProbe(
        run_id=run_id,
        lane_id=str(record.lane_id),
        state_root=state_root,
        repo=repo,
        runtime_root=repo,
        vault=vault,
        private_files=files,
        sealed_ref=str(sealed.artifact_ref),
        sealed_digest=str(sealed.payload.get("sealed_digest") or ""),
        gate=gate,
        provision_argv=provision_argv,
        provision_timeout_s=provision_timeout_s,
    )


# -- entrypoint -------------------------------------------------------------


def _error_line(exc: BaseException, secrets: Sequence[str]) -> str:
    """One line, naming no path the builder may not hold.

    A refusal raised in here is written without paths. Anything else -- a git
    failure, a vault error -- can carry the state root in its message, so the
    known private paths are redacted out and the whole detail is dropped if any
    of them survives.
    """
    detail = str(exc).replace("\n", " ").strip()
    # Longest first, so a vault path is replaced whole rather than being cut
    # into `[redacted]/vaults/run-x.git` by its own state-root prefix.
    ordered = sorted((str(item) for item in secrets if item), key=len, reverse=True)
    for secret in ordered:
        detail = detail.replace(secret, _REDACTED_PATH)
    if any(secret in detail for secret in ordered):
        detail = ""
    if isinstance(exc, ProbeRefused):
        return "sealed probe refused: {0}".format(detail or "unknown reason")
    label = type(exc).__name__
    return "sealed probe could not run: {0}{1}".format(
        label, ": " + detail if detail else ""
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sealed_probe",
        description=(
            "Run this lane's sealed suite against your working tree and print "
            "the counts and redacted failure lines the factory would produce."
        ),
    )
    parser.add_argument("--run", dest="run_id", required=True)
    parser.add_argument("--lane", dest="lane_id", required=True)
    parser.add_argument("--checkout", dest="checkout", default=None)
    args = parser.parse_args(None if argv is None else list(argv))
    checkout = Path(args.checkout) if args.checkout else Path.cwd()
    secrets: list[str] = []
    try:
        # Order is load-bearing. Everything before this call names at most the
        # registry, whose path the builder may hold; everything after it can
        # raise carrying a path under the runtime state root. So the redaction
        # set is seeded from the registry entry FIRST, and only then is
        # anything under that root opened.
        entry = installation_for_run(args.run_id)
        secrets.extend(secret_paths(entry, args.run_id))
        binding = resolve_lane(args.run_id, args.lane_id, installation=entry)
        secrets.extend([str(binding.state_root), str(binding.vault)])
        result = probe_tree(
            checkout,
            repo=binding.repo,
            vault=binding.vault,
            private_files=binding.private_files,
            gate=binding.gate,
            provision_argv=binding.provision_argv,
            provision_timeout_s=binding.provision_timeout_s,
            runtime_root=binding.runtime_root,
            run_id=binding.run_id,
            lane_id=binding.lane_id,
            sealed_ref=binding.sealed_ref,
            sealed_digest=binding.sealed_digest,
        )
    except BaseException as exc:  # noqa: BLE001 -- the message is the contract
        if isinstance(exc, SystemExit):
            raise
        sys.stderr.write(_error_line(exc, secrets) + "\n")
        return 2
    print(render(result))
    return 0
