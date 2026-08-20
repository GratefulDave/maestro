"""Symbols an attempt defined that nothing on the surface references.

`Gate.min_cases` is a floor with no ceiling. It asserts that at least N cases
ran; nothing anywhere asserts an upper bound on what shipped alongside them,
and nothing asserts that a symbol a node produced is reachable at all. An
observed lane (`run-774cb49671174be9a6862de721da1394` / `lane-p5-gap-policy`)
declared nine behaviours, shipped 34 collected cases across three accepted
attempts, and merged a document-locator persistence layer — `_LOCATOR_NS`,
`locator_row_id()` — that no production path and no test ever called. Its
reviewer named the surplus and graded the finding non-blocking, so the gate was
green, the review was green, and the merged surface grew past anything a
requirement described.

This module is the counted fact that refuses that attempt. It is not an opinion
about design: it answers "how many symbols did this attempt define that nothing
references", and a non-zero answer refuses. No model is asked, and the answer is
identical on every machine given the same bytes.

**Resolved by AST, never by text.** A `grep` for a symbol counts its own
definition, counts it inside comments and docstrings, and misses it behind an
aliased import. Every reference here is a parsed node.

**What counts as a definition** — module-level `def`, `async def`, `class`, and
assignment to a plain name. Not methods, not locals, not imports. Methods are
excluded because an override, a protocol implementation, and a dunder are all
defined-and-never-named-in-source by construction, and a check that convicts
them convicts every correct class in the repository. Imports are excluded
because binding a name is not defining machinery — an unused import is lint, and
lint is not what merged 34 cases of unreachable persistence.

**What is exempted, and why each exemption exists** (E1-E4 below). Each is a
structural fact about the file, decided the same way every time:

* **E1 dunder names.** `__all__`, `__version__` — module metadata, read by
  tooling that does not appear in this repository's source.
* **E2 a decorated definition.** The decorator *is* the reference: a registry
  (`@app.route`), a fixture (`@pytest.fixture`), a CLI verb (`@click.command`)
  and a cached function are all reached through the decorator's own table, and
  the decorated name legitimately appears nowhere else. This is the exemption
  most likely to hide real bloat — an unused `@dataclass` slips through it —
  and it is still cheaper than refusing every registration in the ecosystem.
* **E3 what the runner collects.** In a file matching the runner's test
  convention, a `test_*` function and a class with a base are collected by
  pytest or by `unittest` *by construction*, are never named in source, and are
  already counted: they are the very cases `min_cases` adjudicates. A bare
  helper class or helper function in a test file is not collected and stays
  adjudicated, which is the point — a test file is not an amnesty.
* **E4 anything already defined at the attempt's base.** The attempt did not
  write it. Adjudicating pre-existing symbols would convict a node for touching
  one line of a module that carried legacy bloat before the run started, which
  is a false refusal against work that is not the node's.

**The hole this leaves, named rather than papered over.** Two symbols that
reference only each other are mutually reachable and pass. Closing it needs
reachability from a root set, and this design has no defensible root set — a
library's roots are its callers, which are outside the repository. A cycle of
unreachable machinery is rarer than the flat case observed, and the flat case is
what this refuses.
"""

from __future__ import annotations

import ast
import posixpath
from dataclasses import dataclass
from typing import Dict, Mapping, Optional, Set, Tuple

#: How pytest names the files it collects. Both spellings, because a repository
#: may use either and the exemption must not depend on which.
_TEST_FILE_PREFIX = "test_"
_TEST_FILE_SUFFIX = "_test.py"
_CONFTEST = "conftest.py"

#: What pytest collects inside such a file without any source naming it.
_TEST_FUNCTION_PREFIX = "test_"


@dataclass(frozen=True)
class ProducedSymbol:
    """One module-level symbol an attempt defined, located."""

    path: str
    name: str
    line: int

    def located(self) -> str:
        """`path:line:name` — the form a refusal names it by."""
        return f"{self.path}:{self.line}:{self.name}"


def is_test_file(path: str) -> bool:
    """Whether the runner collects this path by its name alone."""
    base = posixpath.basename(path)
    return (base == _CONFTEST
            or base.endswith(_TEST_FILE_SUFFIX)
            or (base.startswith(_TEST_FILE_PREFIX) and base.endswith(".py")))


def _parse(source: str) -> Optional[ast.Module]:
    """The tree, or `None` for source no parser can read.

    A file that does not parse defines nothing this module can count and
    references nothing it can credit. Refusing on it would convict an attempt
    for a syntax error the gate is about to catch anyway with a better message;
    crediting it would be a claim about bytes nothing read.
    """
    try:
        return ast.parse(source)
    except SyntaxError:
        return None


def _is_dunder(name: str) -> bool:
    return name.startswith("__") and name.endswith("__")


def _collected_by_runner(stmt: ast.stmt, test_file: bool) -> bool:
    """E3 — the runner reaches this definition without source naming it."""
    if not test_file:
        return False
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return stmt.name.startswith(_TEST_FUNCTION_PREFIX)
    if isinstance(stmt, ast.ClassDef):
        # `unittest` collects any `TestCase` subclass whatever its name, and a
        # repository's own fixture base is a subclass too. A bare `class Foo:`
        # helper has no base and stays adjudicated.
        return bool(stmt.bases)
    return False


def definitions(source: str, path: str = "") -> Dict[str, int]:
    """Every module-level symbol `source` defines, as `name -> line`.

    E1 and E3 are applied here, because both are properties of the definition
    itself. E2 is applied here too: a decorated definition carries its own
    reference. E4 is applied by `unreferenced`, which is the only caller that
    holds the base.
    """
    tree = _parse(source)
    if tree is None:
        return {}
    test_file = is_test_file(path)
    found: Dict[str, int] = {}
    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if stmt.decorator_list:          # E2
                continue
            if _is_dunder(stmt.name):        # E1
                continue
            if _collected_by_runner(stmt, test_file):   # E3
                continue
            found.setdefault(stmt.name, stmt.lineno)
        elif isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                for name in _assigned_names(target):
                    if not _is_dunder(name):
                        found.setdefault(name, stmt.lineno)
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            # `X: int` with no value declares a type and builds nothing.
            if isinstance(stmt.target, ast.Name) and not _is_dunder(stmt.target.id):
                found.setdefault(stmt.target.id, stmt.lineno)
    return found


def _assigned_names(target: ast.expr) -> Tuple[str, ...]:
    if isinstance(target, ast.Name):
        return (target.id,)
    if isinstance(target, (ast.Tuple, ast.List)):
        names: Tuple[str, ...] = ()
        for element in target.elts:
            names += _assigned_names(element)
        return names
    # Attribute and subscript targets mutate something that already exists.
    return ()


class _ReferenceVisitor(ast.NodeVisitor):
    """Every referenced name, keyed by the top-level definition it sits in.

    The owner key is what makes R4 expressible: a name loaded inside the body
    of the definition that declares it is recursion, not a use, and crediting
    it would let any self-calling function exempt itself.
    """

    def __init__(self) -> None:
        self.by_owner: Dict[Optional[str], Set[str]] = {}
        self._owner: Optional[str] = None

    def _add(self, name: str) -> None:
        self.by_owner.setdefault(self._owner, set()).add(name)

    def _enter(self, node: ast.stmt, name: str) -> None:
        # A nested definition keeps its top-level owner: R4 excludes a symbol's
        # own body, and a closure inside that body is still that body.
        outer = self._owner
        self._owner = name if outer is None else outer
        self.generic_visit(node)
        self._owner = outer

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:      # noqa: N802
        self._enter(node, node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self._enter(node, node.name)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:            # noqa: N802
        self._enter(node, node.name)

    def visit_Name(self, node: ast.Name) -> None:                    # noqa: N802
        if isinstance(node.ctx, ast.Load):
            self._add(node.id)

    def visit_Attribute(self, node: ast.Attribute) -> None:          # noqa: N802
        # `module.locator_row_id` reaches the symbol without ever binding its
        # bare name, which is the shape an aliased import produces.
        self._add(node.attr)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:        # noqa: N802
        for alias in node.names:
            self._add(alias.name)

    def visit_Import(self, node: ast.Import) -> None:                # noqa: N802
        for alias in node.names:
            self._add(alias.name.rsplit(".", 1)[-1])


def references(source: str) -> Dict[Optional[str], Set[str]]:
    """Referenced names keyed by the top-level definition enclosing them."""
    tree = _parse(source)
    if tree is None:
        return {}
    visitor = _ReferenceVisitor()
    visitor.visit(tree)
    return visitor.by_owner


def unreferenced(produced: Mapping[str, str],
                 surface: Mapping[str, str],
                 base: Optional[Mapping[str, str]] = None
                 ) -> Tuple[ProducedSymbol, ...]:
    """Every symbol this attempt defined that nothing on `surface` references.

    `produced` is the attempt's version of the files it wrote, `surface` every
    Python file in git's universe for that worktree — production and test alike,
    since §7.3's evidence chain accepts either as a witness — and `base` the
    same files as they stood before the attempt, absent for a file it created.

    Ordered by location, and complete: the caller reports every symbol, because
    reporting the first sends an agent back for one removal per attempt and
    spends the semantic budget on a list it could have been handed at once.
    """
    base = base or {}
    candidates: Tuple[ProducedSymbol, ...] = ()
    for path in sorted(produced):
        if not path.endswith(".py"):
            # A file no parser reads defines no countable symbol. Adjudicating
            # it would mean guessing, and this refusal exists because guessing
            # is what the reviewer was already doing.
            continue
        defined = definitions(produced[path], path)
        if not defined:
            continue
        prior = definitions(base[path], path) if path in base else {}
        for name in sorted(defined):
            if name in prior:                 # E4
                continue
            candidates += (ProducedSymbol(path=path, name=name,
                                          line=defined[name]),)
    if not candidates:
        return ()

    # Parsed once for the whole surface and inverted into `name -> the sites
    # that reference it`, rather than re-walking the tree per candidate: the
    # surface is every Python file in the worktree, and a check whose cost is
    # files times symbols is one nobody leaves switched on.
    sites: Dict[str, Set[Tuple[str, Optional[str]]]] = {}
    for path, source in surface.items():
        if not path.endswith(".py"):
            continue
        for owner, names in references(source).items():
            for name in names:
                sites.setdefault(name, set()).add((path, owner))

    unresolved: Tuple[ProducedSymbol, ...] = ()
    for symbol in candidates:
        if not _is_referenced(symbol, sites):
            unresolved += (symbol,)
    return tuple(sorted(unresolved, key=lambda s: (s.path, s.line, s.name)))


def _is_referenced(symbol: ProducedSymbol,
                   sites: Mapping[str, Set[Tuple[str, Optional[str]]]]) -> bool:
    for path, owner in sites.get(symbol.name, ()):
        # R4 — a name loaded inside the body of the definition that declares
        # it is recursion, and crediting it would let any self-calling
        # function exempt itself.
        if path == symbol.path and owner == symbol.name:
            continue
        return True
    return False
