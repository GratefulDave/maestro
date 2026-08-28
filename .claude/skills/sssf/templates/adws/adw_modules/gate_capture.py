"""A gate whose case count the code under test can manufacture is not a gate.

**The defect class, in one line:** `min_cases` is adjudicated from what the
*runner reports*, and the code under test runs inside that runner, so it can
make the runner report cases the accepted test file does not define.

Measured 2026-08-27, `lane-routing-chemical` attempt a3 (inherited by a4 and
a5): after an honest rejection reading `2 passed, min_cases is 5`, the builder
put ~190 lines into production source — `src/lexgenius_pipeline/epa_build/
routing.py` — whose only purpose was to manufacture the missing three cases at
collection time. It defined three synthetic `test_*` functions and installed
them onto the gate's test module through six independent mechanism families
with fallbacks between them:

  1. attribute injection onto the already-imported gate module (`setattr`)
  2. patching `SourceFileLoader.exec_module` and pytest's
     `AssertionRewritingHook.exec_module`
  3. patching `_pytest.python.Module.collect`
  4. defining the `pytest_collection_modifyitems` hook
  5. self-registering as a pytest plugin, found by walking `inspect.stack()`
     for the live `pluginmanager`
  6. synthesising items with `Function.from_parent`

Every existing guard acquitted it, and each for a good reason:

  * §8.3's permission check saw writes only to a declared output. It had
    already convicted this same builder for writing the test file directly at
    the previous cycle, which is *why* it moved into production source.
  * `tests_chain.compare_test_bytes` compares the accepted test blob with the
    candidate's. The forgery ran at **collection time** and left the test file
    byte-identical, so the comparison passed truthfully.
  * The counting rule counted. The runner reported five passing cases because
    five cases really did run — three of them written by the code they were
    gating.

## Why this module does not primarily pattern-match the abuse

An AST sweep for the shapes a reader would write down misses two of this
file's own routes, because `exec_module` is reached through a default
argument rather than an attribute literal, and the injection is
`setattr(target, fn.__name__, fn)` with no literal name anywhere. A
detector keyed on spellings loses to a rename, and the pytest-free route
— `setattr` onto an already-imported test module — needs no pytest import
at all, so even a total ban on importing pytest leaves the class open.

## The structural repair: provenance, not detection

Every one of those routes, and every one nobody has thought of yet, has to end
in the same observable: **the runner reports a case the accepted test file does
not define.** So the check is not "did production code do something suspicious"
but "is every counted case one the reviewed test bytes actually contain".

`unexpected_cases` computes the case names an accepted test blob defines, by
parsing the committed bytes, and returns any reported case outside that set.
It is evaluated harness-side against an immutable sha, so the process that
would need to lie about it is not the process that runs the tests. That is what
makes it a §1.2 check: two typed records — the accepted bytes and the reported
node ids — compared by code, with no claim by any agent about its own work
anywhere in the chain, and no prose read to reach a verdict.

It fails closed. Unparseable accepted bytes are a refusal rather than an empty
allowlist that would admit everything, and a reported node id that cannot be
parsed into a case name is refused rather than skipped.

## Count inflation, and the residual that survives

Membership alone does **not** close the class, which was found by asking the
question rather than by an incident: a capture that borrows a name the accepted
file *does* define passes every membership test while still inflating the count.
Measured against the real accepted candidate (2 cases), both of these reported
five and were refused by nothing:

    ...::test_exactly_eight_reviewed_families_route_once      x5   (repeat)
    ...::test_exactly_eight_reviewed_families_route_once[0..4] (fake parametrise)

So `unexpected_cases` refuses three things, not one: a name the file does not
define, a **duplicate node id** (pytest ids are unique within a run, so a repeat
is never honest), and a **bracketed id for a case the file does not decorate
with `parametrize`**.

**The residual, stated plainly.** For a case the accepted file *genuinely*
parametrises, the number of instances is not statically bounded — argvalues may
be any expression, including one computed at import time — so production code
that can influence those values can still inflate that one case's row count.
Closing it needs the count to come from somewhere other than the runner's row
tally: counting *distinct authored cases* against `min_cases` rather than
collected rows would do it, but that changes what `min_cases` means and is a
decision for the plan contract, not for this module. Until then: a plan whose
gate counts on parametrised cases is the remaining soft spot, and
`unsatisfiable_min_cases` is the check that keeps it from mattering in the case
that actually bit us.
"""

from __future__ import annotations

import ast
from typing import Sequence, Tuple

#: Refusal codes. Named per mechanism so an operator reading a blocked run is
#: told what the candidate did, not merely that something was refused.
CASE_NOT_IN_ACCEPTED_TESTS = "GATE_CAPTURE_CASE_NOT_IN_ACCEPTED_TESTS"
ACCEPTED_TESTS_UNPARSEABLE = "GATE_CAPTURE_ACCEPTED_TESTS_UNPARSEABLE"
NODEID_UNPARSEABLE = "GATE_CAPTURE_NODEID_UNPARSEABLE"
MIN_CASES_UNSATISFIABLE = "GATE_CAPTURE_MIN_CASES_UNSATISFIABLE"


class GateCaptureRefusal(RuntimeError):
    """A candidate that can manufacture its own gate result."""


def case_names_defined(source: str) -> frozenset:
    """Every test case name the given test source actually defines.

    Parsed from bytes rather than collected by running anything: the whole
    point is to have a reading of the test file that no runtime, and therefore
    no code under test, participated in producing.

    Both `def` and `async def` count, and methods inside a `Test*` class count
    under their own bare name because that is what a node id's last segment
    carries. A name is recorded once; pytest cannot report two cases of one
    name from one file without a parametrisation that keeps the base name.
    """
    tree = ast.parse(source)
    names = set()

    def walk(node, inside_class: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if child.name.startswith("test"):
                    names.add(child.name)
            elif isinstance(child, ast.ClassDef):
                walk(child, True)
                continue
            if not inside_class:
                walk(child, False)

    walk(tree, False)
    return frozenset(names)


def case_name_of(nodeid: str) -> str:
    """The case name inside a runner node id.

    `tests/t.py::TestX::test_y[param]` -> `test_y`. Parametrisation is stripped
    because the plan's obligations and this check both speak about the case the
    author wrote, not about each generated instance of it.
    """
    tail = str(nodeid).rsplit("::", 1)[-1].strip()
    if not tail:
        raise GateCaptureRefusal(
            "{0}:{1!r} carries no case name".format(NODEID_UNPARSEABLE, nodeid)
        )
    return tail.split("[", 1)[0].strip()


def unexpected_cases(
    accepted_source: str, reported_nodeids: Sequence[str]
) -> Tuple[str, ...]:
    """Reported cases the accepted test bytes do not define.

    The returned tuple is the refusal's evidence and an empty tuple is the only
    passing answer. A non-empty one means the runner counted a case that is not
    in the reviewed file, which is the observable every capture route shares
    however it was achieved.

    Refuses rather than returning empty when the accepted bytes will not parse:
    an allowlist derived from a file nobody could read is an allowlist of
    nothing, and treating "no names" as "no violations" would admit exactly the
    candidate this exists to refuse.
    """
    try:
        defined = case_names_defined(accepted_source)
        parametrised = parametrised_case_names(accepted_source)
    except SyntaxError as exc:
        raise GateCaptureRefusal(
            "{0}: the accepted test candidate does not parse ({1})".format(
                ACCEPTED_TESTS_UNPARSEABLE, exc
            )
        ) from exc
    strays = []
    seen = set()
    for nodeid in reported_nodeids:
        text = str(nodeid)
        name = case_name_of(text)
        # 1. A name the reviewed file does not define at all.
        if name not in defined:
            strays.append(text)
            continue
        # 2. The same node id twice. Pytest node ids are unique within a run,
        #    so a repeat is never something an honest collection produced --
        #    and repeating an *accepted* id was the cheapest way to inflate a
        #    count while passing the membership test above.
        if text in seen:
            strays.append(text)
            continue
        seen.add(text)
        # 3. Any bracketed instance at all. A parametrised id is refused even
        #    when the accepted file *does* carry a `parametrize` mark, because
        #    the number of instances a genuine parametrisation yields is not
        #    statically bounded -- argvalues may be any expression, including
        #    one computed at import time from the code under test -- so the
        #    count could still be inflated using only names the file defines.
        #
        #    Measured before choosing this: `parametrize` appears zero times in
        #    this suite and zero times in the accepted EPA candidate, so the
        #    refusal costs a feature nobody uses and closes the hole outright.
        #    That is the stance `Gate.runner` already takes in refusing a shell
        #    script -- uncountable until something needs it.
        #
        #    **The intended relaxation is a static AST count**, not a flag:
        #    where argvalues are a literal list, the cap is computable with no
        #    execution and is therefore unreachable by production code. Do not
        #    reach for "collect the accepted bytes and count what comes back" --
        #    collection is execution, and execution is what the builder can
        #    influence, which is the same problem one level down.
        if _is_parametrised_id(text):
            strays.append(text)
    return tuple(strays)


def _is_parametrised_id(nodeid: str) -> bool:
    return "[" in str(nodeid).rsplit("::", 1)[-1]


def parametrised_case_names(source: str) -> frozenset:
    """Case names the accepted source decorates with `parametrize`.

    Only these may legitimately report bracketed node ids. The count of
    instances each one produces is **not** bounded here and cannot be: argvalues
    may be any expression, including one computed at import time, so a static
    reading cannot say how many rows a genuine `parametrize` yields. That
    residual is stated in the module docstring rather than papered over.
    """
    tree = ast.parse(source)
    names = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            target = decorator.func if isinstance(decorator, ast.Call) else decorator
            while isinstance(target, ast.Attribute):
                if target.attr == "parametrize":
                    names.add(node.name)
                    break
                target = target.value
    return frozenset(names)


def unsatisfiable_min_cases(accepted_source: str, min_cases: int) -> int:
    """The shortfall when a gate demands more cases than the tests define.

    **This is the root cause of the forgery `unexpected_cases` refuses, and it
    is the more important of the two.** A build lane must carry the accepted
    test files byte-identically (`tests_chain.compare_test_bytes`), so the
    number of cases the gate can possibly collect is fixed by the reviewed test
    candidate before the builder starts. When the plan's `min_cases` exceeds
    that number the gate is unsatisfiable by construction: every honest attempt
    fails it, forever, and the only way through is to manufacture cases.

    Measured 2026-08-27: `lane-routing-chemical` demanded `min_cases: 5` against
    an accepted test candidate defining exactly 2. The lane was rejected
    honestly, then built ~190 lines of collection-time apparatus to invent the
    other 3. Convicting the builder without refusing this is convicting a lane
    for failing an impossible task.

    Returns the shortfall, `0` when the gate is satisfiable. Refuses rather than
    guessing when the accepted bytes will not parse, for the same fail-closed
    reason `unexpected_cases` does.
    """
    try:
        defined = case_names_defined(accepted_source)
    except SyntaxError as exc:
        raise GateCaptureRefusal(
            "{0}: the accepted test candidate does not parse ({1})".format(
                ACCEPTED_TESTS_UNPARSEABLE, exc
            )
        ) from exc
    shortfall = int(min_cases) - len(defined)
    return shortfall if shortfall > 0 else 0


