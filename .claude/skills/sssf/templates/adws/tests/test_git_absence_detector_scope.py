"""§7.5's git-absence rule, enforced over every function that runs git.

§7.5: "Only git's documented not-found exit code means 'the object is absent.'
Every other nonzero exit is ENVIRONMENTAL, never a fact about the repository.
Without it, a transient git failure is recorded as a missing object, and a
deterministic read over git objects — which every eligibility obligation
depends on (§6.4) — silently returns a wrong answer instead of failing."

`retry_policy.find_ungated_git_absence` is the detector written for that rule,
and `tests/test_retry_policy.py` points it at `retry_policy.py` and nowhere
else. `retry_policy.py` does not shell out to git; eleven other modules do.

**Scope was not the only blind spot, and not the one that mattered.** The live
instance was `plan_validate.blob_at`:

    return out if code == 0 else None

Pointing the existing detector at that source returns `[]`. Two reasons, both
about shape rather than about which file it reads:

1. It visits `ast.If` only, and that line is an `ast.IfExp`. It is never
   looked at.
2. Its absence test requires an identifier containing `absent`, `missing`, or
   `not_found` in the branch body. `return None` — the ordinary Python
   spelling of absence, and the one that function used — contains no such
   name.

A third gap sits underneath both: the rule it enforces is "the test is an `==`
comparison", and `code == 0` satisfies that while meaning the opposite. Zero
is success. Absence taken on the *else* side of `code == 0` is absence taken
for every nonzero exit, which is the exact defect, expressed in the one shape
the detector accepts.

So widening the existing detector's inputs would have reported clean over the
real bug. This file enforces the rule directly instead: it finds the functions
that touch git, finds the names bound from a git call's exit status inside
them, and convicts any branch that resolves to absence unless it is reached by
comparing that status to a documented not-found code. Both the `if` and the
ternary spellings, and `None`/`False` as absence alongside the named markers.

Narrowing the input to git-facing functions is what keeps this precise without
an allowlist. Run over whole modules, an absence check convicts ordinary code:
`maestro._execute_run` holds a local named `proven_absent` for pane
quiescence, which has nothing to do with git and which any name-matching
absence rule flags on sight. Suppressing that with an allowlist is how a guard
dies, so the exit-status binding is required instead.

`retry_policy.py` is not this lane's file, so the detector there is left as it
is and this stands beside it. Replacing it is a change for whoever owns it;
the evidence is in this docstring.
"""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
if str(ADWS) not in sys.path:
    sys.path.insert(0, str(ADWS))

#: Helpers that hand back git's raw exit status, so a caller of one of these
#: is holding an ungated status and owes the rule below.
GIT_RAW_CALLS = frozenset({"_git", "git", "run_git", "git_output"})

#: Helpers that have already adjudicated the status and return a fact. A
#: caller may treat their answer as true — that is their whole job — so a
#: `stored is None` test after `blob_at` is correct handling, not this defect.
#: Conflating the two is what made the first draft of this file convict
#: `plan_validate._evidence_typed_against_git` for doing the right thing.
GIT_ADJUDICATED_CALLS = frozenset({
    "blob_at", "commit_exists", "branch_exists", "object_at",
})

GIT_HELPERS = GIT_RAW_CALLS | GIT_ADJUDICATED_CALLS

#: Names that read as "this holds a process exit status".
STATUS_NAMES = ("code", "returncode", "status", "rc", "exit")

#: Identifier fragments that read as "absent" — the named half of the answer.
ABSENCE_MARKERS = ("absent", "absence", "missing", "not_found", "notfound")


def _production_modules() -> list:
    out = [path for path in sorted((ADWS / "adw_modules").glob("*.py"))
           if "__pycache__" not in path.parts]
    out.append(ADWS / "maestro.py")
    return out


def _name_of(node) -> str:
    if isinstance(node, ast.Attribute):
        return node.attr
    return getattr(node, "id", "") or ""


def _is_git_call(node: ast.Call) -> bool:
    """Any call that reaches git — used to decide a function is git-facing."""
    if _name_of(node.func) in GIT_HELPERS:
        return True
    return _is_raw_git_call(node)


def _is_raw_git_call(node: ast.Call) -> bool:
    """A call that yields git's own exit status rather than a decided fact."""
    if _name_of(node.func) in GIT_RAW_CALLS:
        return True
    if _name_of(node.func) in GIT_ADJUDICATED_CALLS:
        return False
    for inner in ast.walk(node):
        if isinstance(inner, ast.Constant) and inner.value == "git":
            return True
    return False


def _git_facing_functions(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if any(isinstance(inner, ast.Call) and _is_git_call(inner)
               for inner in ast.walk(node)):
            found.append(node)
    return found


def _status_names(function) -> set:
    """Names bound from a git call that hold its exit status.

    Requiring the binding is what removes the false positives. A local called
    `proven_absent` that never touched a git exit code is not this defect.
    """
    names = set()
    for node in ast.walk(function):
        if not isinstance(node, ast.Assign):
            continue
        value = node.value
        if not (isinstance(value, ast.Call) and _is_raw_git_call(value)):
            continue
        for target in node.targets:
            elements = (target.elts
                        if isinstance(target, (ast.Tuple, ast.List))
                        else [target])
            for element in elements:
                name = _name_of(element)
                if name and any(part in name.lower() for part in STATUS_NAMES):
                    names.add(name)
                elif name:
                    # `result = subprocess.run(...)` — the status is an
                    # attribute of the result rather than a name of its own.
                    names.add(name)
    return names


def _yields_absence(nodes) -> bool:
    for node in nodes if isinstance(nodes, list) else [nodes]:
        # A keyword argument's constant is configuration, not a conclusion.
        # `_git(..., check=False)` inside a failure branch is not that branch
        # concluding `False`, and reading it as one convicted
        # `worktree.merge_verified_node` for handling a merge conflict.
        keyword_constants = {
            id(keyword.value)
            for sub in ast.walk(node) if isinstance(sub, ast.Call)
            for keyword in sub.keywords
            if isinstance(keyword.value, ast.Constant)
        }
        for sub in ast.walk(node):
            if isinstance(sub, ast.Constant) and sub.value in (None, False):
                if id(sub) in keyword_constants:
                    continue
                return True
            name = _name_of(sub) if isinstance(sub, (ast.Name, ast.Attribute)) else ""
            if name and any(m in name.lower() for m in ABSENCE_MARKERS):
                return True
    return False


def _mentions_status(test, statuses: set) -> bool:
    for sub in ast.walk(test):
        if isinstance(sub, (ast.Name, ast.Attribute)):
            if _name_of(sub) in statuses:
                return True
    return False


def _equality_to_not_found(test) -> bool:
    """The only shape that may conclude absence: the status *equals* a
    documented not-found code. `!= 0` is every failure at once, and `== 0` is
    success — so absence on its else-branch is every failure at once too."""
    if not (isinstance(test, ast.Compare) and len(test.ops) == 1
            and isinstance(test.ops[0], ast.Eq)):
        return False
    right = test.comparators[0]
    if isinstance(right, ast.Constant) and right.value == 0:
        return False   # zero is success, not a not-found code
    return True


def _violations(function) -> list:
    statuses = _status_names(function)
    if not statuses:
        return []
    found = []
    for node in ast.walk(function):
        if isinstance(node, ast.IfExp):
            if not _mentions_status(node.test, statuses):
                continue
            # The ternary's two arms, each with the test that reaches it.
            if _yields_absence(node.body) and not _equality_to_not_found(node.test):
                found.append((node.lineno, "ternary concludes absence"))
            elif _yields_absence(node.orelse) and _equality_to_not_found(node.test):
                continue
            elif _yields_absence(node.orelse):
                found.append((node.lineno,
                              "ternary concludes absence on the else arm"))
        elif isinstance(node, ast.If):
            if not _mentions_status(node.test, statuses):
                continue
            if _yields_absence(node.body) and not _equality_to_not_found(node.test):
                found.append((node.lineno, "branch concludes absence"))
            if node.orelse and _yields_absence(node.orelse) and _equality_to_not_found(node.test):
                found.append((node.lineno,
                              "else-branch concludes absence for every other exit"))
    return found


class GitAbsenceOverEveryGitFacingFunctionTest(unittest.TestCase):

    def test_the_survey_actually_finds_git_facing_code(self):
        """A miscount would make the check below pass vacuously, which is the
        one way this file could lie in the reassuring direction."""
        total = sum(len(_git_facing_functions(path))
                    for path in _production_modules())
        self.assertGreater(
            total, 25,
            "only {} git-facing functions across {} modules — the survey is "
            "broken, not the tree".format(total, len(_production_modules())))

    def test_no_git_facing_function_concludes_absence_from_a_bare_failure(self):
        offenders = []
        for path in _production_modules():
            for function in _git_facing_functions(path):
                for line, detail in _violations(function):
                    offenders.append("{}:{} {}(): {}".format(
                        path.name, line, function.name, detail))
        self.assertEqual(
            [], offenders,
            "a git-facing function concludes that an object is absent from an "
            "exit status that only means git did not succeed (§7.5). A "
            "transient git failure then becomes a durable claim about the "
            "repository, and every eligibility obligation that reads git "
            "objects (§6.4) returns a wrong answer instead of failing.\n"
            + "\n".join(offenders))

    def test_it_convicts_the_instance_that_was_actually_live(self):
        """The red control, and the reason this file is not just a wider
        pointer at the existing detector: that detector returns `[]` on this
        exact source."""
        original = ast.parse(
            "def blob_at(repo, commit, path):\n"
            "    code, out = _git(repo, 'cat-file', 'blob', '%s:%s')\n"
            "    return out if code == 0 else None\n").body[0]
        self.assertTrue(
            _violations(original),
            "the shape that shipped was not convicted")

    def test_it_convicts_the_statement_spelling_too(self):
        planted = ast.parse(
            "def read(repo, spec):\n"
            "    code, out = _git(repo, 'cat-file', 'blob', spec)\n"
            "    if code != 0:\n"
            "        return None\n"
            "    return out\n").body[0]
        self.assertTrue(_violations(planted),
                        "the `!= 0` statement form was not convicted")

    def test_it_accepts_absence_gated_on_a_documented_not_found_code(self):
        clean = ast.parse(
            "def read(repo, spec, not_found_code):\n"
            "    code, out = _git(repo, 'cat-file', 'blob', spec)\n"
            "    if code == not_found_code:\n"
            "        return None\n"
            "    if code != 0:\n"
            "        raise GitReadFailed(spec)\n"
            "    return out\n").body[0]
        self.assertEqual(
            [], _violations(clean),
            "the correct form was convicted, which would make the rule "
            "unsatisfiable")

    def test_an_unrelated_absent_named_local_is_not_a_violation(self):
        """`maestro._execute_run` keeps a `proven_absent` set for pane
        quiescence. Any rule matching on the name alone flags it, and
        allowlisting that is how a guard dies — so the exit-status binding is
        required instead."""
        benign = ast.parse(
            "def run(repo):\n"
            "    proven_absent = set()\n"
            "    subprocess.run(['git', 'worktree', 'add'])\n"
            "    if some_condition:\n"
            "        proven_absent.add(1)\n"
            "    return proven_absent\n").body[0]
        self.assertEqual([], _violations(benign))


if __name__ == "__main__":
    unittest.main()
