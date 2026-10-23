"""The measurement half of a remote suite run: what executes on the runner.

Every test process maestro starts -- a runner's capability probe, a draft's
collection, a suite, a reviewer's probe -- runs here, inside the GitHub Actions
job `remote_suite` dispatches onto the AWS spot fleet, never on the scheduler
host. Each op is the body its call site used to run locally, unchanged, so the
measurement and its parsing are the code the suite already pins; only where the
process starts moved.

Results and the typed refusals the ops raise cross the boundary as JSON
(`encode` / `decode`): a `RunnerUnusable` raised on the runner is a
`RunnerUnusable` at the call site, and the call site's own `except` clauses
decide what it means, exactly as before.

Invoked on the runner as
`python -m adw_modules.remote_job <job.json> <subject> <result.json>`.
"""

from __future__ import annotations

import dataclasses
import importlib
import json
import shutil
import sys
import tempfile
import traceback
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Mapping

from . import probes
from . import provisioning as prov
from . import review_contract as rc
from . import runner_resolution as rr
from . import tests_chain as tc

_PACKAGE = __name__.rsplit(".", 1)[0]

#: Exception and dataclass types a result may name. Anything else is refused
#: by `decode` rather than imported by name off a payload.
_TYPE_MODULES = ("builtins",)


# -- codec -----------------------------------------------------------------


def encode(value: Any) -> Any:
    """A JSON-safe form of an op's result or raised exception."""
    if isinstance(value, BaseException):
        cls = type(value)
        return {
            "__error__": {
                "module": cls.__module__,
                "name": cls.__qualname__,
                "args": [encode(item) for item in value.args],
                "attrs": {
                    key: encode(item) for key, item in vars(value).items()
                },
                "text": str(value),
            }
        }
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        cls = type(value)
        return {
            "__dataclass__": {
                "module": cls.__module__,
                "name": cls.__qualname__,
                "fields": {
                    field.name: encode(getattr(value, field.name))
                    for field in dataclasses.fields(value)
                },
            }
        }
    if isinstance(value, SimpleNamespace):
        return {"__namespace__": {key: encode(item) for key, item in vars(value).items()}}
    if isinstance(value, Mapping):
        return {str(key): encode(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [encode(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def _named_type(module: str, name: str) -> type:
    if module != _PACKAGE and not module.startswith(_PACKAGE + ".") and module not in _TYPE_MODULES:
        raise ValueError("remote result names a foreign type {0}.{1}".format(module, name))
    target: Any = importlib.import_module(module)
    for part in name.split("."):
        target = getattr(target, part)
    if not isinstance(target, type):
        raise ValueError("remote result names a non-type {0}.{1}".format(module, name))
    return target


def decode(value: Any) -> Any:
    """The inverse of `encode`. An encoded exception comes back as an instance."""
    if isinstance(value, list):
        return [decode(item) for item in value]
    if not isinstance(value, dict):
        return value
    if set(value) == {"__error__"}:
        raw = value["__error__"]
        cls = _named_type(str(raw["module"]), str(raw["name"]))
        if not issubclass(cls, BaseException):
            raise ValueError("remote error names a non-exception type")
        args = tuple(decode(item) for item in raw.get("args") or ())
        if raw["module"] == "builtins":
            return cls(*args)
        # Built field by field, not through `__init__`: the remote instance was
        # already constructed once, and its constructor's required arguments
        # are not its attributes.
        error = cls.__new__(cls)
        error.args = args
        error.__dict__.update({k: decode(v) for k, v in (raw.get("attrs") or {}).items()})
        return error
    if set(value) == {"__dataclass__"}:
        raw = value["__dataclass__"]
        cls = _named_type(str(raw["module"]), str(raw["name"]))
        return cls(**{k: decode(v) for k, v in (raw.get("fields") or {}).items()})
    if set(value) == {"__namespace__"}:
        return SimpleNamespace(**{k: decode(v) for k, v in value["__namespace__"].items()})
    return {key: decode(item) for key, item in value.items()}


# -- ops -------------------------------------------------------------------
#
# Each takes the subject tree and the op's decoded arguments. Scratch space is
# allocated outside the subject, so no measurement writes into what it measures.


def _op_resolve(subject: Path, args: Mapping[str, Any]) -> None:
    """The run preflight: does the runner load in this tree at all."""
    rr.resolve(str(args["runner"]), subject, str(args["cwd"]), tuple(args.get("paths") or ()))
    return None


def _op_suite(subject: Path, args: Mapping[str, Any]) -> Any:
    """The accepted suite against one tree: `tests_chain.run_suite`."""
    return tc.run_suite(
        subject,
        tuple(args["paths"]),
        gate=args.get("gate"),
        timeout_s=args.get("timeout_s"),
    )


def _op_draft(subject: Path, args: Mapping[str, Any]) -> Any:
    """A test draft's collection, parent outcomes and harness probe, one tree.

    Resolution runs BEFORE the draft is written, for the reason
    `FactoryScheduler._collect_draft` documents: the probe is a whole-tree
    collect and would otherwise measure the draft instead of the environment.
    """
    gate = args["gate"]
    files = {str(k): str(v) for k, v in dict(args["files"]).items()}
    resolved = rr.resolve(gate.runner, subject, gate.cwd, ())
    rc.write_files(subject, files)
    measured = tc.collect_gate(gate, files, subject)
    ids = rr.collect_cases(resolved, measured, subject)
    outcomes: Mapping[str, bool] = {}
    if measured.declared_cases:
        outcomes = rr.execute_case_outcomes(resolved, measured, subject)
    scratch = Path(tempfile.mkdtemp(prefix="maestro-harness-"))
    try:
        harness = rr.execute_harness_probe(resolved, measured, subject, tuple(files), scratch)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return {"ids": list(ids), "outcomes": dict(outcomes), "harness": harness}


def _op_probes(subject: Path, args: Mapping[str, Any]) -> Any:
    """Resolve one runner over `peers`, then run each of `paths` once."""
    resolved = rr.resolve(str(args["runner"]), subject, ".", paths=list(args["peers"]))
    timeout_s = float(args.get("timeout_s") or rr.EXECUTE_TIMEOUT_S)
    scratch = Path(tempfile.mkdtemp(prefix="maestro-probes-"))
    try:
        return {
            str(path): probes.run_probe(resolved, subject, str(path), scratch, timeout_s)
            for path in args["paths"]
        }
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _op_command(subject: Path, args: Mapping[str, Any]) -> Any:
    """An agent's own test run: the gate runner over the paths it names.

    `collect` lists cases without running them; otherwise the cases run. The
    output and exit status are returned verbatim for the agent to read.
    """
    runner = str(args["runner"])
    cwd = str(args.get("cwd") or ".")
    paths = tuple(str(item) for item in args.get("paths") or ())
    resolved = rr.resolve(runner, subject, cwd, paths)
    gate = SimpleNamespace(runner=runner, argv=paths, cwd=cwd, min_cases=1)
    if args.get("collect"):
        return {"ids": list(rr.collect_cases(resolved, gate, subject)), "returncode": 0}
    return rr.execute_cases(
        resolved,
        gate,
        subject,
        timeout_s=float(args.get("timeout_s") or tc.SUITE_TIMEOUT_S),
    )


OPS: Mapping[str, Callable[[Path, Mapping[str, Any]], Any]] = {
    "command": _op_command,
    "draft": _op_draft,
    "probes": _op_probes,
    "resolve": _op_resolve,
    "suite": _op_suite,
}


def run_op(op: str, subject: Path, args: Mapping[str, Any], provision: Mapping[str, Any]) -> Any:
    """Provision `subject`, then run `op` in it, raising what the op raises.

    Provisioning happens here, where the suite runs, and nowhere else: a tree
    is provisioned after its final materialization, and the subject on the
    runner is materialized by the checkout that precedes this call.
    """
    if op not in OPS:
        raise rc.ReviewContractError("unknown remote suite op {0!r}".format(op))
    argv = tuple(str(item) for item in provision.get("argv") or ())
    packages = tuple(str(item) for item in provision.get("packages") or ())
    if packages:
        prov.set_tree_packages(packages)
    prov.provision_tree(subject, argv, provision.get("timeout_s"))
    # The host tools and devices the suites reach, proven where they run. The
    # run preflight's `resolve` crosses this too, so a missing one refuses the
    # run before any lane is dispatched instead of failing a candidate's suite.
    prov.ensure_suite_requirements(
        subject, provision.get("requirements") or (), provision.get("timeout_s")
    )
    return OPS[op](Path(subject), decode(dict(args)))


def execute(op: str, subject: Path, args: Mapping[str, Any], provision: Mapping[str, Any]) -> Any:
    """`run_op`, with its result or its exception encoded as the job's result."""
    try:
        return {"ok": encode(run_op(op, subject, args, provision))}
    except Exception as exc:  # every failure is the result, typed, never a crash
        return {"error": encode(exc), "traceback": traceback.format_exc()[-4000:]}


def apply_overlay(subject: Path, overlay: Path, deleted: Any) -> None:
    """Put the call site's uncommitted files over the checked-out subject."""
    root = Path(subject).resolve()
    if overlay.is_dir():
        for source in sorted(overlay.rglob("*")):
            if source.is_dir():
                continue
            target = (root / source.relative_to(overlay)).resolve()
            if root not in target.parents:
                raise ValueError("overlay path escapes the subject: {0}".format(source))
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
    for relative in deleted or ():
        target = (root / str(relative)).resolve()
        if root in target.parents and (target.is_file() or target.is_symlink()):
            target.unlink()


def main(argv: list[str]) -> int:
    job_path, subject, result_path = (Path(item) for item in argv[1:4])
    job = json.loads(job_path.read_text(encoding="utf-8"))
    apply_overlay(subject, job_path.parent / "overlay", job.get("deleted"))
    outcome = execute(str(job["op"]), subject, job.get("args") or {}, job.get("provision") or {})
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(outcome, sort_keys=True), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
