"""A redraft of an accepted suite changes only what a finding or a spec change asks.

A tests lane that is sent back after its suite was accepted -- a suite reopen
(a finding attributed to the suite) or a plan amendment that changed its spec --
used to be handed a blank slate. On run 2acd2f5f the tester rewrote the whole
suite and reintroduced defects the accepted suite had already fixed. The
redraft starts from the accepted suite now, and this module measures the
difference per unit: each test function, and each module-level helper, is
hashed by its AST (formatting and comments do not count). A unit that changed or
disappeared must be cited by a finding, or -- after an amendment -- be bound to
a changed spec item. Additions are allowed; they are how a finding or a new
example is discharged.

Pure functions of text. Nothing here reads a ledger, a git object or a clock.
"""

from __future__ import annotations

import ast
import hashlib
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

OUT_OF_SCOPE = "SUITE_REDRAFT_OUT_OF_SCOPE"

#: A changed criterion or interface entry binds a test when the test mentions a
#: word of at least this length that no unchanged spec item uses.
_MIN_TOKEN = 4

_TOKEN = re.compile(r"[A-Za-z]+")


@dataclass(frozen=True)
class Unit:
    path: str
    qual: str
    kind: str  # "test" | "helper"
    digest: str
    start: int
    end: int
    source: str

    @property
    def key(self) -> tuple[str, str]:
        return (self.path, self.qual)


def _digest(node: ast.AST) -> str:
    return hashlib.sha256(ast.dump(node).encode()).hexdigest()


def _is_test(name: str) -> bool:
    return name.startswith("test")


def _span(node: ast.AST) -> tuple[int, int]:
    start = min(
        [node.lineno] + [d.lineno for d in getattr(node, "decorator_list", ())]
    )
    return start, int(getattr(node, "end_lineno", node.lineno))


def _unit(path: str, qual: str, kind: str, node: ast.AST, lines: Sequence[str]) -> Unit:
    start, end = _span(node)
    return Unit(path, qual, kind, _digest(node), start, end, "\n".join(lines[start - 1 : end]))


def _helper_name(node: ast.stmt) -> str:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return node.name
    if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        names = sorted(
            n.id for t in targets for n in ast.walk(t) if isinstance(n, ast.Name)
        )
        if names:
            return "=" + ",".join(names)
    return "{0}@{1}".format(type(node).__name__, _digest(node)[:12])


def units_of(files: Mapping[str, str]) -> dict[tuple[str, str], Unit]:
    """Every comparable unit of a suite, keyed by (path, qualified name)."""
    out: dict[tuple[str, str], Unit] = {}
    for path, text in sorted(files.items()):
        lines = text.splitlines()
        tree = None
        if path.endswith(".py"):
            try:
                tree = ast.parse(text)
            except SyntaxError:
                tree = None
        if tree is None:
            out[(path, "<file>")] = Unit(
                path, "<file>", "helper",
                hashlib.sha256(text.encode()).hexdigest(), 1, max(1, len(lines)), text,
            )
            continue
        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                unit = _unit(path, "import:" + _digest(node)[:12], "import", node, lines)
                out[unit.key] = unit
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                kind = "test" if _is_test(node.name) else "helper"
                unit = _unit(path, node.name, kind, node, lines)
                out[unit.key] = unit
            elif isinstance(node, ast.ClassDef):
                tests = [
                    m for m in node.body
                    if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and _is_test(m.name)
                ] if node.name.startswith("Test") else []
                for method in tests:
                    unit = _unit(path, node.name + "::" + method.name, "test", method, lines)
                    out[unit.key] = unit
                rest = ast.ClassDef(
                    name=node.name, bases=node.bases, keywords=node.keywords,
                    body=[m for m in node.body if m not in tests] or [ast.Pass()],
                    decorator_list=node.decorator_list,
                    **({"type_params": node.type_params} if hasattr(node, "type_params") else {}),
                )
                start, end = _span(node)
                out[(path, node.name)] = Unit(
                    path, node.name, "helper", _digest(rest), start, end,
                    "\n".join(lines[start - 1 : end]),
                )
            else:
                unit = _unit(path, _helper_name(node), "helper", node, lines)
                out[unit.key] = unit
    return out


# ---------------------------------------------------------------- citations


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def _cited(unit: Unit, old: Unit | None, text: str) -> bool:
    """Whether `text` cites the unit by `path:line` or by pytest node id."""
    base = re.escape(unit.path.rsplit("/", 1)[-1])
    for match in re.finditer(r"(?<![A-Za-z0-9_])" + base + r":(\d+)(?:-(\d+))?", text):
        first = int(match.group(1))
        last = int(match.group(2) or first)
        for span in {(unit.start, unit.end), (old.start, old.end) if old else (0, -1)}:
            if first <= span[1] and last >= span[0]:
                return True
    if unit.kind == "test":
        node = re.escape(unit.qual)
        if re.search(
            r"(?<![A-Za-z0-9_])" + base + r"::(?:[A-Za-z_]\w*::)?" + node + r"(?![A-Za-z0-9_])",
            text,
        ) and unit.qual:
            return True
    return False


# --------------------------------------------------------------- spec diff


def _tokens(text: str) -> set[str]:
    return {t.lower() for t in _TOKEN.findall(text) if len(t) >= _MIN_TOKEN}


def _items(contract: Mapping[str, Any] | None) -> dict[str, str]:
    """A contract as {stable key: text}: each criterion, each interface entry."""
    items: dict[str, str] = {}
    contract = contract or {}
    for text in contract.get("acceptance_criteria") or ():
        items["criterion:" + str(text)] = str(text)
    for entry in contract.get("interface") or ():
        text = " ".join(_strings(entry))
        ident = "{0}.{1}".format(
            entry.get("module", "") if isinstance(entry, Mapping) else "",
            entry.get("name", "") if isinstance(entry, Mapping) else "",
        )
        items["interface:" + ident] = text
    return items


def spec_diff(
    old: Mapping[str, Any] | None, new: Mapping[str, Any] | None
) -> dict[str, list[str]]:
    """Criteria and interface entries that were added, removed or changed."""
    before, after = _items(old), _items(new)
    return {
        "added": sorted(k for k in after if k not in before),
        "removed": sorted(k for k in before if k not in after),
        "changed": sorted(k for k in after if k in before and after[k] != before[k]),
    }


def _changed_tokens(
    old: Mapping[str, Any] | None, new: Mapping[str, Any] | None
) -> set[str]:
    before, after = _items(old), _items(new)
    diff = spec_diff(old, new)
    unchanged = {
        k: v for k, v in after.items() if k not in diff["added"] and k not in diff["changed"]
    }
    held = set().union(*(_tokens(v) for v in unchanged.values())) if unchanged else set()
    moved: set[str] = set()
    for key in diff["added"] + diff["changed"]:
        moved |= _tokens(after[key])
    for key in diff["removed"] + diff["changed"]:
        moved |= _tokens(before[key])
    return moved - held


# -------------------------------------------------------------- the check


def out_of_scope(
    baseline: Mapping[str, str],
    draft: Mapping[str, str],
    *,
    finding_texts: Sequence[str],
    old_contract: Mapping[str, Any] | None = None,
    new_contract: Mapping[str, Any] | None = None,
    amendment: bool = False,
) -> list[dict[str, str]]:
    """Findings (REVISE-shaped) for every unit the redraft touched without cause.

    `finding_texts` are the findings the redraft answers. After an `amendment`,
    a test is also in scope when it mentions a word only a changed spec item
    uses. Additions are always in scope after a reopen, and after an amendment
    only when the spec gained or changed something.
    """
    before = units_of(baseline)
    after = units_of(draft)
    spec = spec_diff(old_contract, new_contract) if amendment else None
    bound = _changed_tokens(old_contract, new_contract) if amendment else set()
    texts = list(finding_texts)

    def in_scope(unit: Unit, old: Unit | None) -> bool:
        if any(_cited(unit, old, text) for text in texts):
            return True
        if amendment and unit.kind == "test":
            words = _tokens(unit.source) | _tokens(unit.qual)
            if old is not None:
                words |= _tokens(old.source)
            return bool(words & bound)
        return False

    findings: list[dict[str, str]] = []

    def refuse(unit: Unit, what: str) -> None:
        findings.append(
            {
                "implementation_area": "tests",
                "observed_behavior": (
                    "the redraft {0} {1} {2} ({3}:{4}-{5}), which no finding "
                    "cites{6}".format(
                        what, unit.kind, unit.qual, unit.path, unit.start, unit.end,
                        " and no changed spec item binds" if amendment else "",
                    )
                ),
                "required_behavior": (
                    "the accepted suite is the starting point: restore {0} {1} "
                    "exactly as accepted and change only what a finding "
                    "cites by path:line or node id{2}. Resubmit the accepted "
                    "suite plus the minimal change".format(
                        unit.kind, unit.qual,
                        ", or a test bound to a changed spec item" if amendment else "",
                    )
                ),
                "violated_requirement": OUT_OF_SCOPE,
            }
        )

    for key, old in before.items():
        if old.kind == "import":
            continue
        new = after.get(key)
        if new is None:
            if not in_scope(old, old):
                refuse(old, "removed")
        elif new.digest != old.digest and not in_scope(new, old):
            refuse(new, "changed")
    may_add = (not amendment) or bool(spec and any(spec.values()))
    if not may_add:
        for key, new in after.items():
            if key not in before and new.kind != "import":
                refuse(new, "added")
    return findings
