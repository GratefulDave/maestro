"""The run preflight: every dependency a run can reach, made available or named.

`run start` and `run resume` cross this before any lane is dispatched. Each
dependency is one `Check`: verify it; when it is missing and has a known
source, provision it from that source and verify again; refuse only what is
still missing afterwards. Every check runs, whatever an earlier one answered,
so one refusal names every missing item at once instead of the first of them.

What may be provisioned is fixed by where it comes from, never guessed:

* a tree is created and provisioned through the launch path's own
  `_prepared_cwd` / `provisioning.provision_tree`;
* a Python module the accepted suite imports and the tree lacks is installed
  into that tree's own venv, tree-local, and recorded -- never written to the
  target repository's manifest, which is a published surface;
* a route receipt is captured by `route_admission.admit_routes`, the one
  minting path, signed with the deployment's existing route seed.

What is never provisioned: a key (`keys_dir`; nothing here generates, moves
or rewrites one), a cloud credential, `gh` login, an omp profile's auth, and
anything with no declared source. Those refuse, flagged `human_secret` when
only a person can supply them. A lane's declared outputs and its suite paths
are never needs at all: they are what the tester and builder produce, and the
preflight must not stand in for either.

Records only. Nothing here moves `lane_state`; a refusal stops the run before
the scheduler's first transition, and a pass leaves it to run unchanged.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping, Optional, Sequence, Tuple

VERIFIED = "verified"
ADDED = "added"
REFUSED = "refused"
SKIPPED = "skipped"

#: `lane_id` of an item that belongs to the run, not to one lane.
RUN_WIDE = ""

#: Directories never scanned for a tree's own top-level modules.
_UNSCANNED = frozenset((".git", ".venv", "node_modules", "__pycache__", ".maestro-agent"))


@dataclass(frozen=True)
class PreflightItem:
    """One dependency's outcome. `status` is one of the four module constants."""

    item_id: str
    status: str
    detail: str = ""
    remedy: str = ""
    lane_id: str = RUN_WIDE
    #: Refused because only a person can supply it (a key, a credential, a
    #: login). Meaningful on a refusal only.
    human_secret: bool = False
    #: What was provisioned, when `status` is ADDED.
    added: str = ""

    def as_payload(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Check:
    """One dependency: how to see it, and, when it has a source, how to add it.

    `verify` answers `(present, detail)`. `provision` is called only when it
    is absent, and returns a one-line description of what it added; it may
    raise, and its exception is the refusal's detail. `skip` names why the
    run cannot reach this dependency, and is reported rather than dropped.
    """

    item_id: str
    verify: Callable[[], Tuple[bool, str]]
    remedy: str = ""
    lane_id: str = RUN_WIDE
    provision: Optional[Callable[[], str]] = None
    human_secret: bool = False
    skip: str = ""


@dataclass(frozen=True)
class Proc:
    returncode: int
    stdout: str = ""
    stderr: str = ""


#: `runner(argv, cwd=None, env=None, timeout=None) -> Proc`. Injectable, so a
#: test can fake every external tool; `real_runner` is the production path.
Runner = Callable[..., Proc]


def real_runner(
    argv: Sequence[str],
    *,
    cwd: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
    timeout: Optional[float] = 120.0,
) -> Proc:
    """Run one command, never raising: a missing binary is exit 127."""
    try:
        result = subprocess.run(
            [str(item) for item in argv],
            cwd=None if cwd is None else str(cwd),
            env=None if env is None else dict(env),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Proc(127, "", str(exc))
    return Proc(result.returncode, result.stdout or "", result.stderr or "")


def _first_line(proc: Proc) -> str:
    lines = (proc.stderr or proc.stdout or "").strip().splitlines()
    return lines[0] if lines else "exit {0}".format(proc.returncode)


def evaluate(checks: Iterable[Check], say: Callable[[str, str, str], None]) -> Tuple[PreflightItem, ...]:
    """Verify, provision, re-verify every check. Never stops at a refusal.

    `say(lane_id, message, detail)` is the run's step channel; every
    provisioned and every refused item is said there, one line each.
    """
    items = []
    for check in checks:
        if check.skip:
            items.append(PreflightItem(check.item_id, SKIPPED, check.skip, lane_id=check.lane_id))
            continue
        present, detail = _verified(check.verify)
        if present:
            items.append(PreflightItem(check.item_id, VERIFIED, detail, lane_id=check.lane_id))
            continue
        if check.provision is not None:
            try:
                added = check.provision()
            except Exception as exc:  # the refusal's detail, not a crash
                added = ""
                detail = "{0}; provisioning failed: {1}: {2}".format(
                    detail, type(exc).__name__, exc
                )
            else:
                present, after = _verified(check.verify)
                if present:
                    items.append(
                        PreflightItem(
                            check.item_id, ADDED, after, lane_id=check.lane_id, added=added
                        )
                    )
                    say(check.lane_id, "preflight provisioned {0}".format(check.item_id), added)
                    continue
                detail = "{0}; still missing after provisioning ({1}): {2}".format(
                    detail, added, after
                )
        item = PreflightItem(
            check.item_id,
            REFUSED,
            detail,
            check.remedy,
            lane_id=check.lane_id,
            human_secret=check.human_secret,
        )
        items.append(item)
        say(check.lane_id, "preflight refused {0}".format(check.item_id), detail)
    return tuple(items)


def _verified(verify: Callable[[], Tuple[bool, str]]) -> Tuple[bool, str]:
    try:
        present, detail = verify()
    except Exception as exc:  # a verifier that raises has not verified
        return False, "{0}: {1}".format(type(exc).__name__, exc)
    return bool(present), str(detail)


def refused(items: Sequence[PreflightItem]) -> Tuple[PreflightItem, ...]:
    return tuple(item for item in items if item.status == REFUSED)


def render(items: Sequence[PreflightItem]) -> str:
    """The one report: every refused item, then what was skipped and added."""
    lines = []
    for status, title in ((REFUSED, "refused"), (ADDED, "provisioned"), (SKIPPED, "skipped")):
        group = [item for item in items if item.status == status]
        if not group:
            continue
        lines.append("{0} ({1}):".format(title, len(group)))
        for item in group:
            where = item.lane_id or "run"
            text = "  [{0}] {1}: {2}".format(where, item.item_id, item.added or item.detail)
            if status == REFUSED:
                if item.human_secret:
                    text += " (needs a human)"
                if item.remedy:
                    text += " -- remedy: " + item.remedy
            lines.append(text)
    return "\n".join(lines)


# --- checks -----------------------------------------------------------------


def binary_check(item_id: str, binary: str, remedy: str, *, lane_id: str = RUN_WIDE,
                 which: Callable[[str], Optional[str]] = shutil.which) -> Check:
    def verify() -> Tuple[bool, str]:
        found = which(binary) if not os.path.isabs(binary) else (
            binary if os.access(binary, os.X_OK) else None)
        return (found is not None, found or "{0} not found on PATH".format(binary))

    return Check(item_id, verify, remedy, lane_id=lane_id)


def command_check(item_id: str, argv: Sequence[str], remedy: str, runner: Runner, *,
                  human_secret: bool = False, cwd: Optional[Path] = None,
                  accept: Optional[Callable[[Proc], Tuple[bool, str]]] = None) -> Check:
    """A dependency that is present when one read-only command succeeds."""

    def verify() -> Tuple[bool, str]:
        proc = runner(list(argv), cwd=cwd)
        if accept is not None:
            return accept(proc)
        if proc.returncode == 0:
            return True, " ".join(argv)
        return False, "{0}: {1}".format(" ".join(argv), _first_line(proc))

    return Check(item_id, verify, remedy, human_secret=human_secret)


def readable_file_check(item_id: str, path: Optional[Path], remedy: str, *,
                        human_secret: bool = False, executable: bool = False,
                        parse: Optional[Callable[[bytes], object]] = None,
                        lane_id: str = RUN_WIDE) -> Check:
    def verify() -> Tuple[bool, str]:
        if path is None:
            return False, "not configured"
        target = Path(path)
        if not target.is_file():
            return False, "{0} does not exist".format(target)
        if not os.access(target, os.R_OK):
            return False, "{0} is not readable".format(target)
        if executable and not os.access(target, os.X_OK):
            return False, "{0} is not executable".format(target)
        if parse is not None:
            parse(target.read_bytes())
        return True, str(target)

    return Check(item_id, verify, remedy, lane_id=lane_id, human_secret=human_secret)


def directory_check(item_id: str, path: Path, remedy: str, *, human_secret: bool = False) -> Check:
    def verify() -> Tuple[bool, str]:
        return (Path(path).is_dir(), str(path))

    return Check(item_id, verify, remedy, human_secret=human_secret)


def gh_permission_accept(proc: Proc) -> Tuple[bool, str]:
    """`gh repo view --json viewerPermission`: triage or better can file issues."""
    if proc.returncode != 0:
        return False, _first_line(proc)
    text = proc.stdout or ""
    for level in ("ADMIN", "MAINTAIN", "WRITE", "TRIAGE"):
        if '"{0}"'.format(level) in text:
            return True, level
    return False, "viewerPermission is {0}".format(text.strip() or "empty")


# --- per-lane needs -----------------------------------------------------------


def tree_modules(tree: Path) -> frozenset:
    """Top-level module names the tree itself provides: never a package to fetch.

    A name the suite imports that is also a directory or a `.py` file anywhere
    in the tree is the project's own code, wherever its import root is. Treating
    one as a missing PyPI package would install whatever a stranger published
    under that name.
    """
    names = set()
    for root, dirs, files in os.walk(tree):
        dirs[:] = [name for name in dirs if name not in _UNSCANNED]
        names.update(dirs)
        names.update(name[:-3] for name in files if name.endswith(".py"))
    return frozenset(names)


def output_modules(paths: Iterable[str]) -> frozenset:
    """Every top-level name a declared output could provide once it is built."""
    names = set()
    for raw in paths:
        parts = [part for part in str(raw).replace("\\", "/").split("/") if part]
        for part in parts:
            names.add(part[:-3] if part.endswith(".py") else part)
    return frozenset(names)


def python_needs(
    specifiers: Iterable[str],
    *,
    local: frozenset,
    outputs: frozenset,
) -> Tuple[str, ...]:
    """Top-level third-party modules a suite imports.

    Excluded, in order: relative imports, the standard library, the tree's own
    modules, and anything a declared output provides -- a build lane's output
    missing before it is built is the lane's work, not a need.
    """
    needs = set()
    for specifier in specifiers:
        text = str(specifier)
        if not text or text.startswith("."):
            continue
        top = text.split(".", 1)[0]
        if top in sys.stdlib_module_names or top in local or top in outputs:
            continue
        needs.add(top)
    return tuple(sorted(needs))


def tree_python(tree: Path) -> Path:
    return Path(tree) / ".venv" / "bin" / "python"


_FIND_SPEC = (
    "import importlib.util,sys;"
    "sys.exit(0 if importlib.util.find_spec(sys.argv[1]) is not None else 1)"
)


def module_check(
    lane_id: str,
    module: str,
    tree: Path,
    cwd: Path,
    runner: Runner,
    install: Callable[[str], str],
) -> Check:
    """`module` importable in the tree's venv, else installed there, tree-local."""

    def verify() -> Tuple[bool, str]:
        python = tree_python(tree)
        if not python.exists():
            return False, "{0} has no venv ({1} missing)".format(tree.name, python)
        proc = runner([str(python), "-c", _FIND_SPEC, module], cwd=cwd)
        if proc.returncode == 0:
            return True, "importable in the lane tree"
        return False, "not importable in the lane tree's venv"

    return Check(
        "python:{0}".format(module),
        verify,
        "add {0} to the project's dependency manifest through a reviewed lane".format(module),
        lane_id=lane_id,
        provision=lambda: install(module),
    )


def fixture_check(lane_id: str, relative: str, tree: Path) -> Check:
    """A fixture the plan says exists before the lane runs."""

    def verify() -> Tuple[bool, str]:
        target = (Path(tree) / relative).resolve()
        try:
            target.relative_to(Path(tree).resolve())
        except ValueError:
            return False, "{0} escapes the tree".format(relative)
        return (target.exists(), relative)

    return Check(
        "fixture:{0}".format(relative),
        verify,
        "the plan declares this fixture as already present; commit it to the "
        "target repository or amend the fixture's path",
        lane_id=lane_id,
    )


def declared_fixture_paths(spec: Mapping[str, object]) -> Tuple[str, ...]:
    """`path` of every observed-baseline fixture the lane spec carries."""
    obligations = spec.get("obligations") if isinstance(spec, Mapping) else None
    records = obligations.get("observed_baseline") if isinstance(obligations, Mapping) else None
    paths = []
    for record in records or ():
        if isinstance(record, Mapping):
            path = record.get("path")
            if isinstance(path, str) and path.strip():
                paths.append(path.strip())
    return tuple(sorted(set(paths)))
