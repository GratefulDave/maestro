"""The names a sealed suite binds to, with none of the values it asserts.

Maestro seals acceptance tests so the builder cannot read them, and then never
tells the builder which API the sealed cases actually call. On the WP7 run the
sealed vitest suite calls `paidDpa.buildEntityDpaSurface()` and reads an
`available` key off the result; neither name occurs anywhere in the plan, so the
builder guessed nineteen times and shipped six aliases of the same function in
one file without moving the pass count.

The line this module draws:

    Names are contract. Values are secrets.

`derive_bound_surface` returns module specifiers, imported and dereferenced
symbol names, and object-literal keys. It never returns a string literal, a
number, a regex, a fixture, or anything else an assertion compares against, so
its output can be handed to a builder without unsealing the suite.

The output is built as a *whitelist*: every element is a name this module
affirmatively recognized in a syntactic position that can only hold a name, and
every element is then re-checked against `_IDENTIFIER` / `_SPECIFIER` before it
leaves. Nothing is produced by filtering text down, because a filter that misses
one case leaks a secret while a whitelist that misses one case only costs a
round.

Where a form cannot be parsed with confidence the answer is to return less. A
name this module fails to report costs the builder one round of guessing; a name
it reports wrongly costs the builder its reason to trust any of them.
"""

from __future__ import annotations

import ast
import posixpath
import re
from typing import Any, Iterable, Mapping, NamedTuple

__all__ = ["derive_bound_surface"]


#: Which language a sealed file is read as. This mirrors the runner convention
#: in `tests_chain._file_runner` (`.py` is pytest; the JavaScript family is
#: vitest) but is deliberately *broader* on the JavaScript side: a `.ts` helper
#: sealed beside a `.test.ts` suite does not get a vote on the runner, yet the
#: modules it imports are just as much part of the contract the builder has to
#: satisfy.
_PYTHON_SUFFIX = ".py"
_JAVASCRIPT_EXTENSIONS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts")

#: A symbol or object key must look like an identifier. This is the boundary
#: that keeps values out: `prrCiLow` passes, `summary:read` does not, and no
#: sentence, path, or number can pass at all.
_IDENTIFIER = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")

#: A module specifier is a path or a dotted package name, and nothing else. No
#: whitespace, no quotes, no punctuation an assertion literal would carry.
_SPECIFIER = re.compile(r"^[A-Za-z0-9_@][A-Za-z0-9_.:/\\@-]*$")

_MAX_SPECIFIER_LENGTH = 200
_MAX_IDENTIFIER_LENGTH = 120


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def derive_bound_surface(files: Mapping[str, str]) -> dict:
    """The bound surface of a sealed file set: names only, never values.

    `files` maps a sealed test path to that file's content. The return value is

        {"modules": [{"specifier": str, "symbols": [str, ...]}, ...],
         "object_keys": [str, ...]}

    sorted at every level, with empty lists rather than missing keys, and never
    `None`. A file whose language is unrecognized, or which does not parse,
    contributes nothing rather than contributing a guess.
    """
    modules: dict[str, set[str]] = {}
    object_keys: set[str] = set()

    for path in sorted(files):
        content = files[path]
        if not isinstance(content, str):
            continue
        language = _language_of(path)
        if language == "python":
            found_modules, found_keys = _python_surface(content)
        elif language == "javascript":
            found_modules, found_keys = _javascript_surface(str(path), content)
        else:
            continue
        for specifier, symbols in found_modules.items():
            modules.setdefault(specifier, set()).update(symbols)
        object_keys |= found_keys

    clean: dict[str, set[str]] = {}
    for specifier, symbols in modules.items():
        if not _is_specifier(specifier):
            continue
        clean.setdefault(specifier, set()).update(
            name for name in symbols if _is_identifier(name)
        )

    return {
        "modules": [
            {"specifier": specifier, "symbols": sorted(clean[specifier])}
            for specifier in sorted(clean)
        ],
        "object_keys": sorted(name for name in object_keys if _is_identifier(name)),
    }


def _language_of(path: str) -> str | None:
    name = str(path).rsplit("/", 1)[-1]
    if name.endswith(_PYTHON_SUFFIX):
        return "python"
    for extension in _JAVASCRIPT_EXTENSIONS:
        if name.endswith(extension):
            return "javascript"
    return None


def _is_identifier(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) <= _MAX_IDENTIFIER_LENGTH
        and bool(_IDENTIFIER.match(value))
    )


def _is_specifier(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) <= _MAX_SPECIFIER_LENGTH
        and bool(_SPECIFIER.match(value))
    )


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------
#
# The keys a sealed case reads off a value it got back from the code under
# construction are the return contract, and they are the half of the surface an
# import list cannot carry: the WP7 suite calls `buildEntityDpaSurface(...)` and
# then reads `.publicCharts`, `.paidPanel` and `.publicBand` off the result,
# none of which is an imported name.
#
# Tracking is seeded only from a *first-party* symbol — one the sealed file
# imports from a module inside the repository rather than from a package — so a
# fixture parsed out of JSON with `json.loads` never contributes its schema, and
# `readFileSync` never contributes `String.prototype`. It follows one value at a
# time through calls and member reads, and it never reads anything but a name.


def _first_party_symbols(modules: Mapping[str, Iterable[str]]) -> set[str]:
    """Symbols of modules that live in this repository rather than in a package.

    A path specifier carries a `/`, and a Python package path carries a `.`;
    `vitest`, `astro`, `node:fs`, `httpx` and `pathlib` carry neither. The
    distinction only ever narrows what is tracked, so a first-party module this
    misreads as a package costs coverage and never correctness.
    """
    symbols: set[str] = set()
    for specifier, names in modules.items():
        if "/" in specifier or "." in specifier:
            symbols.update(names)
    return symbols


def _python_result_keys(tree: ast.AST, symbols: set[str]) -> set[str]:
    assignments: list[tuple[str, ast.expr]] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            assignments.append((node.targets[0].id, node.value))
        elif (
            isinstance(node, ast.AnnAssign)
            and node.value is not None
            and isinstance(node.target, ast.Name)
        ):
            assignments.append((node.target.id, node.value))

    tracked: set[str] = set()
    for _ in range(len(assignments) + 1):
        grew = False
        for name, value in assignments:
            if name in tracked:
                continue
            if _python_calls_symbol(value, symbols) or (
                _python_value_root(value) in tracked
            ):
                tracked.add(name)
                grew = True
        if not grew:
            break

    keys: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            if _python_value_root(node.value) in tracked:
                keys.add(node.attr)
        elif isinstance(node, ast.Subscript):
            if _python_value_root(node.value) in tracked:
                key = node.slice
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    keys.add(key.value)
    return keys


def _python_value_root(node: ast.expr) -> str | None:
    """The name a chain of calls, attributes and subscripts is rooted at."""
    current: ast.expr = node
    while True:
        if isinstance(current, ast.Await):
            current = current.value
        elif isinstance(current, ast.Call):
            current = current.func
        elif isinstance(current, (ast.Attribute, ast.Subscript)):
            current = current.value
        elif isinstance(current, ast.Name):
            return current.id
        else:
            return None


def _python_calls_symbol(node: ast.expr, symbols: set[str]) -> bool:
    current: ast.expr = node
    while isinstance(current, ast.Await):
        current = current.value
    if not isinstance(current, ast.Call):
        return False
    func = current.func
    if isinstance(func, ast.Attribute):
        return func.attr in symbols
    if isinstance(func, ast.Name):
        return func.id in symbols
    return False


# ---------------------------------------------------------------------------
# Python
# ---------------------------------------------------------------------------


def _python_surface(source: str) -> tuple[dict[str, set[str]], set[str]]:
    """Modules, symbols and asserted dict keys from one Python sealed file."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return {}, set()

    modules: dict[str, set[str]] = {}
    #: A dotted *local* path (`mapping`, `app.mapping`) to the module specifier
    #: it names. Attribute chains are matched against this longest-prefix-first,
    #: so `import app.mapping` followed by `app.mapping.normalize_dpa_data`
    #: yields the symbol and not the intermediate package name.
    bindings: dict[str, str] = {}

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                specifier = alias.name
                modules.setdefault(specifier, set())
                bindings[alias.asname or alias.name] = specifier
        elif isinstance(node, ast.ImportFrom):
            specifier = _python_from_specifier(node)
            if specifier is None:
                continue
            bucket = modules.setdefault(specifier, set())
            for alias in node.names:
                if alias.name != "*":
                    bucket.add(alias.name)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        chain = _python_attribute_chain(node)
        if chain is None:
            continue
        for cut in range(len(chain) - 1, 0, -1):
            prefix = ".".join(chain[:cut])
            specifier = bindings.get(prefix)
            if specifier is not None:
                modules.setdefault(specifier, set()).add(chain[cut])
                break

    keys = _python_asserted_keys(tree)
    keys |= _python_result_keys(tree, _first_party_symbols(modules))
    return modules, keys


def _python_from_specifier(node: ast.ImportFrom) -> str | None:
    """The specifier a `from ... import ...` names.

    A relative import keeps its leading dots rather than being resolved into a
    package path: which directory is the package root is not knowable from the
    sealed file set, and a wrong absolute module name is worse than a relative
    one that is exactly what the source says.
    """
    if node.level:
        # Relative specifiers are excluded by `_SPECIFIER` and so never reach
        # the output; reporting the module the builder cannot act on would be a
        # guess about the package root.
        return None
    return node.module or None


def _python_attribute_chain(node: ast.Attribute) -> list[str] | None:
    """`a.b.c` as `["a", "b", "c"]`, or `None` when the base is not a name."""
    parts: list[str] = []
    current: ast.expr = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return None
    parts.append(current.id)
    parts.reverse()
    return parts


def _python_asserted_keys(tree: ast.AST) -> set[str]:
    """Keys of the dict literals this file compares against.

    Two sources, and only two. A dict literal written inside a comparison, and a
    module-level `NAME = {...}` that a comparison names — the second because the
    pinned expectations in a real suite are hoisted into constants
    (`assert data["metrics"] == EXPECTED_METRICS`) and the keys of that constant
    are precisely the contract the builder has to produce.
    """
    literals: dict[str, ast.Dict] = {}
    for node in ast.walk(tree):
        target: ast.expr | None = None
        value: ast.expr | None = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            target, value = node.target, node.value
        if isinstance(target, ast.Name) and isinstance(value, ast.Dict):
            literals.setdefault(target.id, value)

    keys: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        for side in [node.left, *node.comparators]:
            _python_collect_side(side, literals, keys)
    return keys


def _python_collect_side(
    side: ast.expr, literals: Mapping[str, ast.Dict], keys: set[str]
) -> None:
    for inner in ast.walk(side):
        if isinstance(inner, ast.Dict):
            _python_collect_dict(inner, keys)
        elif isinstance(inner, ast.Name):
            literal = literals.get(inner.id)
            if literal is not None:
                _python_collect_dict(literal, keys)


def _python_collect_dict(node: ast.Dict, keys: set[str]) -> None:
    """String keys of a dict literal and of every dict nested inside it.

    Values are walked into only to reach further *keys*: `ast.walk` visits the
    nested `ast.Dict` nodes, and nothing but a key position is ever read.
    """
    for inner in ast.walk(node):
        if not isinstance(inner, ast.Dict):
            continue
        for key in inner.keys:
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                keys.add(key.value)


# ---------------------------------------------------------------------------
# JavaScript / TypeScript
# ---------------------------------------------------------------------------
#
# There is no TypeScript parser in this runtime, so this is a bounded tokenizer
# plus pattern matching over the token stream. It is bounded in both senses: it
# recognizes a fixed list of forms, and it answers nothing at all for anything
# outside that list.


class _Token(NamedTuple):
    kind: str  # "name" | "str" | "num" | "punct" | "template" | "regex"
    value: str


_JS_PUNCTUATORS = (
    ">>>=",
    "...",
    "===",
    "!==",
    "**=",
    "<<=",
    ">>=",
    "&&=",
    "||=",
    "??=",
    ">>>",
    "=>",
    "==",
    "!=",
    "<=",
    ">=",
    "&&",
    "||",
    "??",
    "?.",
    "++",
    "--",
    "+=",
    "-=",
    "*=",
    "/=",
    "%=",
    "&=",
    "|=",
    "^=",
    "**",
    "<<",
    ">>",
)

#: After one of these a `/` opens a regular expression rather than dividing.
#: Getting this wrong is not cosmetic: a regex read as division leaves its body
#: to be tokenized, and a body such as `/path:\s*"\/"/` would then open a string
#: that swallows the rest of the file.
_REGEX_PRECEDING_KEYWORDS = frozenset(
    {
        "return",
        "typeof",
        "instanceof",
        "in",
        "of",
        "new",
        "delete",
        "void",
        "throw",
        "case",
        "do",
        "else",
        "yield",
        "await",
    }
)

_SIMPLE_ESCAPES = {
    "n": "\n",
    "t": "\t",
    "r": "\r",
    "b": "\b",
    "f": "\f",
    "v": "\v",
    "0": "\0",
}


def _tokenize_javascript(source: str) -> list[_Token]:
    tokens: list[_Token] = []
    index = 0
    length = len(source)

    while index < length:
        char = source[index]

        if char in " \t\r\n\f\v ﻿":
            index += 1
            continue

        if source.startswith("//", index):
            end = source.find("\n", index)
            index = length if end == -1 else end + 1
            continue

        if source.startswith("/*", index):
            end = source.find("*/", index + 2)
            index = length if end == -1 else end + 2
            continue

        if char in "'\"":
            value, index = _read_js_string(source, index)
            tokens.append(_Token("str", value))
            continue

        if char == "`":
            index = _skip_js_template(source, index)
            tokens.append(_Token("template", ""))
            continue

        if char == "/" and _regex_allowed(tokens):
            index = _skip_js_regex(source, index)
            tokens.append(_Token("regex", ""))
            continue

        if char.isdigit() or (
            char == "." and index + 1 < length and source[index + 1].isdigit()
        ):
            start = index
            while index < length and (
                source[index].isalnum() or source[index] in "._"
            ):
                index += 1
            tokens.append(_Token("num", source[start:index]))
            continue

        if char.isalpha() or char in "_$":
            start = index
            while index < length and (source[index].isalnum() or source[index] in "_$"):
                index += 1
            tokens.append(_Token("name", source[start:index]))
            continue

        for punctuator in _JS_PUNCTUATORS:
            if source.startswith(punctuator, index):
                tokens.append(_Token("punct", punctuator))
                index += len(punctuator)
                break
        else:
            tokens.append(_Token("punct", char))
            index += 1

    return tokens


def _regex_allowed(tokens: list[_Token]) -> bool:
    if not tokens:
        return True
    last = tokens[-1]
    if last.kind == "name":
        return last.value in _REGEX_PRECEDING_KEYWORDS
    if last.kind in ("str", "num", "template", "regex"):
        return False
    return last.value not in (")", "]", "}")


def _read_js_string(source: str, index: int) -> tuple[str, int]:
    quote = source[index]
    index += 1
    out: list[str] = []
    length = len(source)
    while index < length:
        char = source[index]
        if char == "\\":
            index += 1
            if index >= length:
                break
            escape = source[index]
            if escape == "u" and source.startswith("u{", index):
                end = source.find("}", index)
                if end != -1:
                    out.append(_codepoint(source[index + 2 : end]))
                    index = end + 1
                    continue
            if escape == "u":
                out.append(_codepoint(source[index + 1 : index + 5]))
                index += 5
                continue
            if escape == "x":
                out.append(_codepoint(source[index + 1 : index + 3]))
                index += 3
                continue
            out.append(_SIMPLE_ESCAPES.get(escape, escape))
            index += 1
            continue
        if char == quote:
            return "".join(out), index + 1
        if char == "\n":
            # An unterminated string. Stop here rather than consuming the file.
            return "".join(out), index
        out.append(char)
        index += 1
    return "".join(out), index


def _codepoint(digits: str) -> str:
    try:
        return chr(int(digits, 16))
    except ValueError:
        return ""


def _skip_js_template(source: str, index: int) -> int:
    """Past a template literal, including its `${...}` holes.

    Nothing inside a template is tokenized. A member access written inside an
    interpolation is therefore not reported, which is the intended trade: a
    template hole can nest arbitrary expressions, strings and further templates,
    and half-parsing it is how a tokenizer starts emitting garbage.
    """
    index += 1
    length = len(source)
    while index < length:
        char = source[index]
        if char == "\\":
            index += 2
            continue
        if char == "`":
            return index + 1
        if source.startswith("${", index):
            index += 2
            depth = 1
            while index < length and depth:
                inner = source[index]
                if inner == "\\":
                    index += 2
                    continue
                if inner in "'\"":
                    _, index = _read_js_string(source, index)
                    continue
                if inner == "`":
                    index = _skip_js_template(source, index)
                    continue
                if inner == "{":
                    depth += 1
                elif inner == "}":
                    depth -= 1
                index += 1
            continue
        index += 1
    return index


def _skip_js_regex(source: str, index: int) -> int:
    index += 1
    length = len(source)
    in_class = False
    while index < length:
        char = source[index]
        if char == "\\":
            index += 2
            continue
        if char == "[":
            in_class = True
        elif char == "]":
            in_class = False
        elif char == "/" and not in_class:
            index += 1
            while index < length and (source[index].isalpha()):
                index += 1
            return index
        elif char == "\n":
            return index
        index += 1
    return index


# --- token-stream helpers ---------------------------------------------------


_OPENERS = {"(": ")", "[": "]", "{": "}"}


def _is_punct(tokens: list[_Token], index: int, *values: str) -> bool:
    return (
        0 <= index < len(tokens)
        and tokens[index].kind == "punct"
        and tokens[index].value in values
    )


def _is_name(tokens: list[_Token], index: int, *values: str) -> bool:
    if not (0 <= index < len(tokens) and tokens[index].kind == "name"):
        return False
    return not values or tokens[index].value in values


def _match_bracket(tokens: list[_Token], index: int) -> int:
    """The index just past the bracket that opens at `index`."""
    closer = _OPENERS[tokens[index].value]
    depth = 0
    while index < len(tokens):
        token = tokens[index]
        if token.kind == "punct":
            if token.value in _OPENERS:
                depth += 1
            elif token.value in (")", "]", "}"):
                depth -= 1
                if depth == 0:
                    if token.value != closer:
                        return len(tokens)
                    return index + 1
        index += 1
    return len(tokens)


# --- object literal keys ----------------------------------------------------


def _collect_keys_in_range(
    tokens: list[_Token], start: int, end: int, keys: set[str]
) -> None:
    index = start
    while index < end:
        if _is_punct(tokens, index, "{"):
            index = min(_collect_object_keys(tokens, index, keys), end)
            continue
        index += 1


def _collect_object_keys(tokens: list[_Token], index: int, keys: set[str]) -> int:
    """Keys of the object literal opening at `index`; returns the index past it.

    Key positions and value positions are parsed apart on purpose. A scan that
    took every `name` followed by `,` or `}` as a shorthand key would report
    `false` and `null` out of `{ entitled: false, metrics: null }`, and a scan
    that took every `str` would report the values.
    """
    end = _match_bracket(tokens, index)
    index += 1
    while index < end - 1:
        if _is_punct(tokens, index, ",", ";"):
            index += 1
            continue
        if _is_punct(tokens, index, "..."):
            index = _skip_object_value(tokens, index + 1, end, keys)
            continue
        # `get foo() {}` / `set foo() {}` / `async foo() {}` — the modifier is
        # not the key.
        if (
            _is_name(tokens, index, "get", "set", "async", "static")
            and _is_name(tokens, index + 1)
            and not _is_punct(tokens, index + 1, ":")
        ):
            index += 1
            continue
        if _is_punct(tokens, index, "*"):
            index += 1
            continue
        if _is_punct(tokens, index, "["):
            # A computed key is an expression, not a name.
            index = _match_bracket(tokens, index)
            if _is_punct(tokens, index, ":"):
                index = _skip_object_value(tokens, index + 1, end, keys)
            continue
        token = tokens[index]
        if token.kind in ("name", "str", "num"):
            following = index + 1
            if _is_punct(tokens, following, ":"):
                if token.kind in ("name", "str"):
                    keys.add(token.value)
                index = _skip_object_value(tokens, following + 1, end, keys)
                continue
            if _is_punct(tokens, following, "("):
                # A method shorthand: the name is still a key.
                if token.kind in ("name", "str"):
                    keys.add(token.value)
                index = _skip_object_value(tokens, following, end, keys)
                continue
            if token.kind == "name" and (
                _is_punct(tokens, following, ",", "}") or following >= end - 1
            ):
                keys.add(token.value)
                index = following
                continue
        index = _skip_object_value(tokens, index, end, keys)
    return end


def _skip_object_value(
    tokens: list[_Token], index: int, end: int, keys: set[str]
) -> int:
    """Past one property value, collecting the keys of objects nested in it."""
    while index < end - 1:
        if _is_punct(tokens, index, ","):
            return index + 1
        if _is_punct(tokens, index, "{"):
            index = _collect_object_keys(tokens, index, keys)
            continue
        if _is_punct(tokens, index, "(", "["):
            stop = _match_bracket(tokens, index)
            _collect_keys_in_range(tokens, index + 1, max(stop - 1, index + 1), keys)
            index = stop
            continue
        if _is_punct(tokens, index, "}"):
            return index
        index += 1
    return index


#: The matchers whose argument is a literal shape the implementation must
#: produce. `toBe`/`toContain` compare against a value, so their arguments are
#: never read.
_SHAPE_MATCHERS = frozenset({"toEqual", "toStrictEqual", "toMatchObject"})


def _javascript_object_keys(tokens: list[_Token]) -> set[str]:
    keys: set[str] = set()
    literals = _javascript_literal_bindings(tokens)

    for index, token in enumerate(tokens):
        if token.kind != "name" or token.value not in _SHAPE_MATCHERS:
            continue
        if not _is_punct(tokens, index - 1, ".", "?."):
            continue
        if not _is_punct(tokens, index + 1, "("):
            continue
        open_paren = index + 1
        close = _match_bracket(tokens, open_paren)
        start, stop = open_paren + 1, max(close - 1, open_paren + 1)
        _collect_keys_in_range(tokens, start, stop, keys)
        # `toEqual(EXPECTED)` — one bare name naming a literal declared in this
        # file. Anything more complex than a bare name is not resolved.
        if stop - start == 1 and tokens[start].kind == "name":
            region = literals.get(tokens[start].value)
            if region is not None:
                _collect_keys_in_range(tokens, region[0], region[1], keys)
    return keys


def _javascript_literal_bindings(
    tokens: list[_Token],
) -> dict[str, tuple[int, int]]:
    """`const NAME = { ... }` / `= [ ... ]`, as the token range of the literal."""
    bindings: dict[str, tuple[int, int]] = {}
    for index, token in enumerate(tokens):
        if token.kind != "name" or token.value not in ("const", "let", "var"):
            continue
        if not _is_name(tokens, index + 1):
            continue
        if not _is_punct(tokens, index + 2, "="):
            continue
        opener = index + 3
        if not _is_punct(tokens, opener, "{", "["):
            continue
        bindings.setdefault(
            tokens[index + 1].value, (opener, _match_bracket(tokens, opener))
        )
    return bindings


# --- imports and namespace bindings -----------------------------------------


def _javascript_surface(
    path: str, source: str
) -> tuple[dict[str, set[str]], set[str]]:
    tokens = _tokenize_javascript(source)
    modules: dict[str, set[str]] = {}
    #: local binding name -> module specifier
    namespaces: dict[str, str] = {}

    directory = posixpath.dirname(str(path).replace("\\", "/"))

    for index, token in enumerate(tokens):
        if token.kind != "name" or token.value != "import":
            continue
        if _is_punct(tokens, index - 1, ".", "?."):
            continue  # `foo.import` is not an import statement
        _read_import(tokens, index, directory, modules, namespaces)

    loaders = _javascript_loader_helpers(tokens)
    for index in range(len(tokens)):
        binding = _read_module_binding(tokens, index, directory, loaders)
        if binding is None:
            continue
        local, specifier = binding
        modules.setdefault(specifier, set())
        if local is not None:
            namespaces[local] = specifier

    for index, token in enumerate(tokens):
        if token.kind != "name":
            continue
        specifier = namespaces.get(token.value)
        if specifier is None:
            continue
        if _is_punct(tokens, index - 1, ".", "?."):
            continue  # `something.paidDpa` is not the binding
        cursor = index + 1
        while _is_punct(tokens, cursor, "!", "?"):
            cursor += 1
        if _is_punct(tokens, cursor, ".", "?.") and _is_name(tokens, cursor + 1):
            modules.setdefault(specifier, set()).add(tokens[cursor + 1].value)

    keys = _javascript_object_keys(tokens)
    keys |= _javascript_result_keys(tokens, _first_party_symbols(modules))
    return modules, keys


def _read_import(
    tokens: list[_Token],
    index: int,
    directory: str,
    modules: dict[str, set[str]],
    namespaces: dict[str, str],
) -> None:
    """One `import` statement, in the forms this module claims to handle."""
    cursor = index + 1
    if _is_punct(tokens, cursor, "("):
        return  # a dynamic import expression; handled as a module binding
    if tokens[cursor : cursor + 1] and tokens[cursor].kind == "str":
        specifier = _resolve_specifier(tokens[cursor].value, directory, False)
        if specifier:
            modules.setdefault(specifier, set())
        return
    if _is_name(tokens, cursor, "type"):
        cursor += 1

    symbols: list[str] = []
    locals_named: list[str] = []

    while cursor < len(tokens):
        if _is_punct(tokens, cursor, "{"):
            close = _match_bracket(tokens, cursor)
            symbols.extend(_read_named_import_clause(tokens, cursor + 1, close - 1))
            cursor = close
        elif _is_punct(tokens, cursor, "*"):
            if _is_name(tokens, cursor + 1, "as") and _is_name(tokens, cursor + 2):
                locals_named.append(tokens[cursor + 2].value)
                cursor += 3
            else:
                return
        elif _is_name(tokens, cursor) and tokens[cursor].value != "from":
            # A default import. Its local name is bound, because the default
            # export of a module is routinely an object whose properties are
            # exactly as much part of the contract as a named export.
            locals_named.append(tokens[cursor].value)
            symbols.append("default")
            cursor += 1
        else:
            break
        if _is_punct(tokens, cursor, ","):
            cursor += 1
            continue
        break

    if not _is_name(tokens, cursor, "from"):
        return
    if not (cursor + 1 < len(tokens) and tokens[cursor + 1].kind == "str"):
        return
    specifier = _resolve_specifier(tokens[cursor + 1].value, directory, False)
    if not specifier:
        return
    modules.setdefault(specifier, set()).update(symbols)
    for local in locals_named:
        namespaces[local] = specifier


def _read_named_import_clause(
    tokens: list[_Token], start: int, end: int
) -> list[str]:
    """`{ a, b as c, type d }` — the names as the module exports them."""
    symbols: list[str] = []
    cursor = start
    while cursor < end:
        if _is_punct(tokens, cursor, ","):
            cursor += 1
            continue
        if _is_name(tokens, cursor, "type") and _is_name(tokens, cursor + 1):
            cursor += 1
        if not _is_name(tokens, cursor) and tokens[cursor].kind != "str":
            cursor += 1
            continue
        symbols.append(tokens[cursor].value)
        cursor += 1
        if _is_name(tokens, cursor, "as") and _is_name(tokens, cursor + 1):
            cursor += 2
    return symbols


def _javascript_loader_helpers(tokens: list[_Token]) -> set[str]:
    """Functions declared in this file whose body performs a dynamic `import()`.

    A sealed suite that must stay red at the parent commit cannot import an
    unwritten module statically — the collector would fail instead of the
    assertion — so it routes the import through a helper that builds the
    specifier at runtime. That helper's literal argument names a module just as
    much as an `import` statement does, but only because the helper provably
    hands it to `import()`. A function that merely takes a string is not
    treated as one.
    """
    helpers: set[str] = set()
    for index, token in enumerate(tokens):
        name: str | None = None
        params: int | None = None
        if token.kind == "name" and token.value == "function":
            if _is_name(tokens, index + 1) and _is_punct(tokens, index + 2, "("):
                name, params = tokens[index + 1].value, index + 2
        elif token.kind == "name" and token.value in ("const", "let", "var"):
            if _is_name(tokens, index + 1) and _is_punct(tokens, index + 2, "="):
                cursor = index + 3
                if _is_name(tokens, cursor, "async"):
                    cursor += 1
                if _is_name(tokens, cursor, "function"):
                    cursor += 1
                    if _is_name(tokens, cursor):
                        cursor += 1
                if _is_punct(tokens, cursor, "("):
                    name, params = tokens[index + 1].value, cursor
        if name is None or params is None:
            continue
        body = _function_body_range(tokens, params)
        if body is None:
            continue
        start, stop = body
        for cursor in range(start, stop):
            if (
                tokens[cursor].kind == "name"
                and tokens[cursor].value == "import"
                and _is_punct(tokens, cursor + 1, "(")
            ):
                helpers.add(name)
                break
    return helpers


def _function_body_range(
    tokens: list[_Token], params: int
) -> tuple[int, int] | None:
    """The token range of the `{ ... }` body following a parameter list."""
    cursor = _match_bracket(tokens, params)
    # A return type annotation or an `=>` may sit between `)` and `{`.
    limit = min(cursor + 60, len(tokens))
    while cursor < limit:
        if _is_punct(tokens, cursor, "{"):
            return cursor, _match_bracket(tokens, cursor)
        if _is_punct(tokens, cursor, ";") or _is_name(tokens, cursor, "function"):
            return None
        cursor += 1
    return None


def _read_module_binding(
    tokens: list[_Token], index: int, directory: str, loaders: set[str]
) -> tuple[str | None, str] | None:
    """`const NS = await import("m")` / `await loadHelper("m")`, and bare calls.

    Returns `(local binding or None, specifier)`.
    """
    callee: int | None = None
    local: str | None = None

    if (
        tokens[index].kind == "name"
        and tokens[index].value in ("const", "let", "var")
        and _is_name(tokens, index + 1)
        and _is_punct(tokens, index + 2, "=")
    ):
        local = tokens[index + 1].value
        cursor = index + 3
        if _is_name(tokens, cursor, "await"):
            cursor += 1
        callee = cursor
    elif tokens[index].kind == "name" and tokens[index].value == "await":
        callee = index + 1
    else:
        return None

    if not _is_name(tokens, callee):
        return None
    name = tokens[callee].value
    if name != "import" and name not in loaders:
        return None
    if not _is_punct(tokens, callee + 1, "("):
        return None
    argument = callee + 2
    if not (argument < len(tokens) and tokens[argument].kind == "str"):
        return None
    if not _is_punct(tokens, argument + 1, ")", ","):
        return None

    specifier = _resolve_specifier(
        tokens[argument].value, directory, name != "import"
    )
    if not specifier:
        return None
    return local, specifier


def _resolve_specifier(raw: str, directory: str, from_loader: bool) -> str | None:
    """A module specifier as a repository path where that is knowable.

    A relative specifier is resolved against the directory of the sealed file,
    so `../api/bff` in `src/lib/seo/paid-dpa.test.ts` becomes `src/lib/api/bff`
    and names a file the builder can go and write. A package specifier
    (`vitest`, `node:fs`, `astro`) is left exactly as written.

    A loader helper's argument is resolved as relative even when it carries no
    `./`, because a helper exists precisely to defer resolution of a path the
    bundler must not see — a bare name handed to `import()` would resolve to a
    package, which is not what an unwritten module in this repository is.
    """
    specifier = raw.strip()
    if not specifier or "\n" in specifier:
        return None
    if specifier.startswith("./") or specifier.startswith("../"):
        return _normalize_relative(specifier, directory)
    if from_loader and not specifier.startswith("/") and ":" not in specifier:
        return _normalize_relative("./" + specifier, directory)
    return specifier


def _normalize_relative(specifier: str, directory: str) -> str | None:
    joined = posixpath.normpath(posixpath.join(directory, specifier))
    if joined.startswith("..") or joined in (".", "/"):
        return None
    return joined or None


def _javascript_result_keys(tokens: list[_Token], symbols: set[str]) -> set[str]:
    """Keys read off a value returned by a first-party symbol this file calls."""
    assignments: list[tuple[str, str | None, str | None]] = []
    for index, token in enumerate(tokens):
        if token.kind != "name" or token.value not in ("const", "let", "var"):
            continue
        if not (_is_name(tokens, index + 1) and _is_punct(tokens, index + 2, "=")):
            continue
        root, callee = _javascript_initializer(tokens, index + 3)
        assignments.append((tokens[index + 1].value, root, callee))

    tracked: set[str] = set()
    for _ in range(len(assignments) + 1):
        grew = False
        for name, root, callee in assignments:
            if name in tracked:
                continue
            if (callee is not None and callee in symbols) or (
                root is not None and root in tracked
            ):
                tracked.add(name)
                grew = True
        if not grew:
            break

    keys: set[str] = set()
    for index, token in enumerate(tokens):
        if token.kind != "name" or token.value not in tracked:
            continue
        if _is_punct(tokens, index - 1, ".", "?."):
            continue
        _javascript_walk_chain(tokens, index + 1, keys)
    return keys


def _javascript_initializer(
    tokens: list[_Token], index: int
) -> tuple[str | None, str | None]:
    """`(await a.b.c(...))` as its root name `a` and the name it calls, `c`."""
    cursor = index
    while _is_punct(tokens, cursor, "(") or _is_name(tokens, cursor, "await", "new"):
        cursor += 1
    if not _is_name(tokens, cursor):
        return None, None
    root = tokens[cursor].value
    last = root
    cursor += 1
    while True:
        while _is_punct(tokens, cursor, "!", "?"):
            cursor += 1
        if _is_punct(tokens, cursor, ".", "?.") and _is_name(tokens, cursor + 1):
            last = tokens[cursor + 1].value
            cursor += 2
            continue
        break
    return root, (last if _is_punct(tokens, cursor, "(") else None)


def _javascript_walk_chain(tokens: list[_Token], index: int, keys: set[str]) -> None:
    """Every property name read off a tracked value, following the whole chain."""
    cursor = index
    while cursor < len(tokens):
        while _is_punct(tokens, cursor, "!", "?"):
            cursor += 1
        if _is_punct(tokens, cursor, ".", "?.") and _is_name(tokens, cursor + 1):
            keys.add(tokens[cursor + 1].value)
            cursor += 2
            continue
        if _is_punct(tokens, cursor, "["):
            stop = _match_bracket(tokens, cursor)
            if stop - cursor == 3 and tokens[cursor + 1].kind == "str":
                keys.add(tokens[cursor + 1].value)
            cursor = stop
            continue
        if _is_punct(tokens, cursor, "("):
            cursor = _match_bracket(tokens, cursor)
            continue
        return
