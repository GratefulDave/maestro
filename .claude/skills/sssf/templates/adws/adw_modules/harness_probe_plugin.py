"""pytest plugin for the draft gate's harness probe. Never imported by maestro.

`runner_resolution.execute_harness_probe` copies this file into a scratch
directory outside the tree under test and loads it with `-p` into one pytest
run of a tests lane's draft at the parent commit. It depends on the standard
library and pytest only, because it runs in the tree's interpreter, not the
factory's.

For every case that fails in setup or call it writes one JSON line: the
exception's class names, the innermost frame inside the tree (site-packages and
virtualenvs excluded), and whether any production frame was entered before the
failure. A production frame is a call into a `.py` file in the tree that is not
one of the draft's files and not a `conftest.py`. Classification is the
factory's (`scheduler._harness_defects`); this file only measures.
"""

from __future__ import annotations

import json
import os
import sys
import traceback

import pytest

_TREE = os.path.realpath(os.environ.get("MAESTRO_HARNESS_TREE") or "/nonexistent")
_DRAFT = frozenset(
    os.path.realpath(os.path.join(_TREE, path))
    for path in json.loads(os.environ.get("MAESTRO_HARNESS_DRAFT") or "[]")
)
_REPORT = os.environ.get("MAESTRO_HARNESS_REPORT") or ""
_OUTSIDE = ("site-packages", "dist-packages", ".venv", "venv", "node_modules")
_kinds: dict = {}
_entered = [False]


def _in_tree(filename: str) -> str:
    """The tree-relative path of `filename`, or "" when it is not project code."""
    path = os.path.realpath(filename)
    if not path.startswith(_TREE + os.sep):
        return ""
    rel = os.path.relpath(path, _TREE)
    if any(part in _OUTSIDE for part in rel.split(os.sep)):
        return ""
    return rel


def _production(filename: str) -> bool:
    known = _kinds.get(filename)
    if known is None:
        rel = _in_tree(filename)
        known = bool(
            rel
            and rel.endswith(".py")
            and os.path.join(_TREE, rel) not in _DRAFT
            and os.path.basename(rel) != "conftest.py"
        )
        _kinds[filename] = known
    return known


def _profile(frame, event, arg):  # noqa: ARG001 - sys.setprofile signature
    if event == "call" and _production(frame.f_code.co_filename):
        _entered[0] = True
        sys.setprofile(None)


def _write(row: dict) -> None:
    if not _REPORT:
        return
    with open(_REPORT, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")


def pytest_configure(config):  # noqa: ARG001 - pytest hook signature
    _write({"loaded": True})


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):  # noqa: ARG001 - pytest hook signature
    _entered[0] = False
    sys.setprofile(_profile)


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_call(item):  # noqa: ARG001 - pytest hook signature
    if not _entered[0]:
        sys.setprofile(_profile)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    sys.setprofile(None)
    outcome = yield
    report = outcome.get_result()
    if call.when not in ("setup", "call") or not report.failed or call.excinfo is None:
        return
    frames = [
        (rel, frame.lineno)
        for frame in traceback.extract_tb(call.excinfo.tb)
        for rel in (_in_tree(frame.filename),)
        if rel
    ]
    entered = _entered[0] or any(
        _production(os.path.join(_TREE, rel)) for rel, _line in frames
    )
    value = call.excinfo.value
    target = getattr(value, "obj", None) if isinstance(value, AttributeError) else None
    _write(
        {
            "nodeid": item.nodeid,
            "phase": call.when,
            "exception": [cls.__name__ for cls in type(value).__mro__],
            "message": str(value)[:300],
            "file": frames[-1][0] if frames else "",
            "line": frames[-1][1] if frames else 0,
            "production_entered": entered,
            "attribute_of_module": type(target).__name__ == "module",
        }
    )
