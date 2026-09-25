"""Every module under ``adw_modules/`` is imported by something in the same runtime copy.

The failure this catches is *work that exists but is wired to nothing*. On 2026-09-10
``adw_modules/tree_env.py`` was written in a deployment so that a child process started
inside a provisioned tree runs in that tree's environment. It was never brought into the
template, so a template->deployment mirror overwrote its three importers
(``runner_resolution.py``, ``provisioning.py``, ``tests_chain.py``) with versions that had
never heard of it. ``runtime_sync`` does not delete files, so the module survived, imported
by nothing, and nothing failed. Twenty-two hours later a lane spent three review rounds and
a ``NO_PROGRESS`` park on ``ModuleNotFoundError: No module named 'uvicorn'``, blamed on the
builder.

``MAESTRO_architecture.md`` §3.6 B15 already holds this principle one level down: a check
whose field has zero readers is a build failure. This applies it to modules.

What counts as an importer, and why
-----------------------------------
The search space is the whole runtime copy: ``adw_modules/`` itself, ``tools/``, ``tests/``,
and the top-level entry points (``maestro.py`` and its ``adw_*.py`` siblings). **A module
imported only by a test still counts as imported.** That is a deliberate decision and it is
the weaker of the two available readings, chosen because the defect being caught is total
disconnection -- a module no file in the copy names at all. A module that only its own test
imports is a different and much milder problem (dead product code with live test coverage),
it is visible to anyone who greps for it, and folding it in here would make this check fire
on shapes it was not built to catch and get an allowlist bolted on within a week.

A module loaded by path rather than by an ``import`` statement also counts. The
live example in this copy: ``tests/test_artifact_factory_smoke.py`` reaches
``adw_modules/tools/artifact_factory_smoke.py`` through
``importlib.util.spec_from_file_location``. So a string literal that is exactly
a module's filename, or a path ending in it, is an importer. The match is exact
on purpose -- prose naming ``adw_modules/deliver.py`` inside a docstring must
not count, or the check is satisfied by anything ever written about a module.

Imports are resolved statically with ``ast``; nothing is imported, so the check is safe to
run in any copy, and it makes no path assumption beyond its own location, because it ships
to deployments and runs there too.
"""

from __future__ import annotations

import ast
from pathlib import Path

RUNTIME_ROOT = Path(__file__).resolve().parent.parent
PACKAGE = "adw_modules"

# ``__init__.py`` is a package marker: it is imported by the act of importing anything in
# its package, so it can never have an explicit importer and is not evidence of anything.
EXEMPT_NAMES = {"__init__.py"}


def _python_files(root: Path) -> list[Path]:
    return sorted(
        p
        for p in root.rglob("*.py")
        if "__pycache__" not in p.parts and p.is_file()
    )


def _candidates(base: Path, parts: tuple[str, ...]) -> set[Path]:
    """Files a dotted module path under ``base`` could resolve to."""
    if not parts:
        return set()
    return {
        base.joinpath(*parts).with_suffix(".py"),
        base.joinpath(*parts) / "__init__.py",
    }


def _targets_of(source: Path, tree: ast.AST, root: Path) -> set[Path]:
    """Every file in this runtime copy that ``source``'s import statements name."""
    hits: set[Path] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            # import adw_modules.x  /  import adw_modules.x as y
            for alias in node.names:
                parts = tuple(alias.name.split("."))
                if parts[0] == PACKAGE:
                    hits |= _candidates(root, parts)
        elif isinstance(node, ast.ImportFrom):
            module_parts = tuple((node.module or "").split(".")) if node.module else ()
            if node.level:
                # from . import x  /  from .x import y  /  from ..x import y
                base = source.parent
                for _ in range(node.level - 1):
                    base = base.parent
                if not base.is_relative_to(root):
                    continue
            elif module_parts and module_parts[0] == PACKAGE:
                # from adw_modules import x  /  from adw_modules.x import y
                base = root
            else:
                continue
            hits |= _candidates(base, module_parts)
            # ``from <pkg> import name`` may name a submodule rather than an attribute.
            for alias in node.names:
                if alias.name != "*":
                    hits |= _candidates(base, module_parts + (alias.name,))
    return hits


def _imports_importlib(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(a.name.split(".")[0] == "importlib" for a in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] == "importlib":
                return True
    return False


def _path_literals_of(tree: ast.AST) -> set[str]:
    """String constants naming a module file, in a file that can load one by path.

    Two filters, and both are needed. The literal must be exactly a filename or a path
    ending in one, so prose mentioning ``adw_modules/deliver.py`` inside a docstring does
    not count. And the file must import ``importlib``, which is the only way a module gets
    loaded from a path -- without that, ``self.write(tree, "adw_modules/deliver.py", ...)``
    in a parity test's synthetic fixture tree would read as an importer of the real module.
    """
    if not _imports_importlib(tree):
        return set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        text = node.value
        if not text.endswith(".py") or len(text.splitlines()) != 1:
            continue
        tail = text.rsplit("/", 1)[-1]
        if tail and " " not in text.strip() and text.strip() == text:
            names.add(tail)
    return names


def find_orphan_modules(root: Path) -> list[Path]:
    """Modules under ``<root>/adw_modules`` that no other file in ``root`` imports."""
    package_root = root / PACKAGE
    modules = {
        p.resolve()
        for p in _python_files(package_root)
        if p.name not in EXEMPT_NAMES
    }

    by_filename: dict[str, set[Path]] = {}
    for module in modules:
        by_filename.setdefault(module.name, set()).add(module)

    imported: set[Path] = set()
    for source in _python_files(root):
        try:
            tree = ast.parse(source.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        here = source.resolve()
        targets = _targets_of(source, tree, root)
        for filename in _path_literals_of(tree):
            targets |= by_filename.get(filename, set())
        for target in targets:
            target = target.resolve()
            if target != here:
                imported.add(target)

    return sorted(modules - imported)


def test_every_adw_module_is_imported_by_something_in_this_copy() -> None:
    orphans = find_orphan_modules(RUNTIME_ROOT)
    if not orphans:
        return
    listing = "\n".join(
        f"  - {p.relative_to(RUNTIME_ROOT)}" for p in orphans
    )
    raise AssertionError(
        "These modules exist in this runtime copy and no file in it imports them:\n"
        f"{listing}\n\n"
        f"Runtime copy: {RUNTIME_ROOT}\n"
        "Searched for importers in adw_modules/, tools/, tests/ and the top-level "
        "entry points. An import from a test counts.\n\n"
        "Each one is either (a) work that was disconnected -- most often by a mirror "
        "that overwrote its importers, since runtime_sync never deletes the orphaned "
        "file -- in which case restore the import at the call site that needs it; or "
        "(b) work that was never wired up, in which case delete the file. Do not add "
        "it to an allowlist: a module nothing imports cannot run, so a run that depends "
        "on it fails somewhere else, under another actor's name."
    )
