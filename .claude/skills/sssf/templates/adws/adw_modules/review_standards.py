"""The Standards axis of code review: a fixed hygiene baseline.

Maestro's code reviewer judges one axis today -- Spec: does the candidate do
what the lane plan asked, measured against the sealed suite. That axis is
strong and it is the only one there is, so a candidate that passes its sealed
suite with a duplicated forty-line block, a string standing in for a domain
type, and a function named ``process`` merges without a word said.

This module carries the second axis. It is deliberately *not* a second
authority: a Standards finding is a judgement call about hygiene, it is capped
at ``WARNING``, it rides in ``advisory_findings``, and it can never send a
green candidate back to ``BUILDING``. The two reports are never merged and
never re-ranked against each other.

The baseline is Fowler's twelve smells plus one deep-module item, each stated
as *what it is -> how to fix*, so the reviewer has something to act on rather
than a label to attach.
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

__all__ = [
    "STANDARDS_AXIS",
    "STANDARDS_SMELLS",
    "STANDARDS_RULES",
    "STANDARDS_RUBRIC",
    "STANDARDS_FILENAMES",
    "discover_standards_files",
    "standards_section",
]

STANDARDS_AXIS = "standards"

#: ``(name, what it is, how to fix)`` for every baseline item. The first twelve
#: are Fowler's smells; the thirteenth is the deep-module judgement call from
#: mattpocock's codebase-design notes.
STANDARDS_SMELLS: tuple[tuple[str, str, str], ...] = (
    (
        "Mysterious Name",
        "a name does not say what the thing is or does, so a reader has to "
        "read the body to find out (`process`, `data`, `handle`, `tmp2`)",
        "rename it after the behaviour or the domain concept it carries; if no "
        "honest name fits, the thing is doing more than one job -- split it "
        "first, then name each part",
    ),
    (
        "Duplicated Code",
        "the same structure appears in more than one place, so a change has to "
        "be made in each of them and one of them will be missed",
        "extract the common structure into one named function or type and call "
        "it from both sites; when the copies differ slightly, parameterise the "
        "difference rather than keeping both",
    ),
    (
        "Feature Envy",
        "a function is more interested in another module's data than its own "
        "-- it reaches across to read several fields of one object to compute "
        "something about that object",
        "move the function to the module that owns the data, or move the part "
        "of it that envies the data and leave the rest behind",
    ),
    (
        "Data Clumps",
        "the same group of values travels together through signature after "
        "signature, and never means anything apart",
        "make the clump a type with a name, and pass the type; the signatures "
        "shorten and the invariant between the values gets somewhere to live",
    ),
    (
        "Primitive Obsession",
        "a domain concept is represented by a string, int, or dict, so its "
        "rules are re-checked at every use site and drift apart",
        "introduce a small type for the concept and put the validation and the "
        "operations on it; the primitive stays at the boundary only",
    ),
    (
        "Repeated Switches",
        "the same switch or if/elif chain over the same set of cases appears "
        "in several places, so adding a case means finding all of them",
        "replace the switch with polymorphism or a lookup keyed on the case, "
        "so a new case is added in exactly one place",
    ),
    (
        "Shotgun Surgery",
        "one conceptual change forces small edits in many modules at once",
        "gather the scattered behaviour into a single module so the next such "
        "change has one site",
    ),
    (
        "Divergent Change",
        "one module is edited for several unrelated reasons, so unrelated "
        "concerns are coupled through it",
        "split the module along the axes of change, one module per reason to "
        "change",
    ),
    (
        "Speculative Generality",
        "an abstraction, hook, parameter, or interface exists for a case no "
        "caller has -- it is carried, tested, and read for nothing",
        "delete it; reintroduce the generality when the second real caller "
        "arrives and shows its shape",
    ),
    (
        "Message Chains",
        "a caller walks a chain of objects (`a.b().c().d()`) and so is coupled "
        "to the whole path, not just its ends",
        "ask the first object for what is actually wanted, and let it do the "
        "walking behind its own interface",
    ),
    (
        "Middle Man",
        "a class or module does nothing but delegate; most of its methods "
        "forward straight to the same collaborator",
        "let the caller talk to the collaborator directly and remove the "
        "pass-through, keeping only the members that add something",
    ),
    (
        "Refused Bequest",
        "a subclass or implementation inherits an interface it does not want "
        "and stubs, ignores, or raises on part of it",
        "replace inheritance with delegation, or narrow the base interface to "
        "what every implementation genuinely supports",
    ),
    (
        "Shallow module",
        "a module's interface is nearly as large as its implementation, so "
        "importing it costs about as much understanding as inlining it would; "
        "the interface is the test surface, and a wide one makes every caller "
        "depend on internals",
        "deepen it by moving behaviour behind a smaller interface -- fewer "
        "exported names, fewer parameters, more decided inside -- until the "
        "caller states intent and the module owns the mechanism",
    ),
)

#: The three rules that bound the axis. They are what keep it a hygiene report
#: rather than a second gate.
STANDARDS_RULES: tuple[str, ...] = (
    "A documented repository standard overrides this baseline. Where the repo "
    "declares a convention, judge against the repo and say so; the baseline "
    "applies only where the repo documents nothing.",
    "Every standards finding is a judgement call, never a hard violation. "
    "Phrase it as one (\"possible Feature Envy\"), locate it at the hunk, and "
    "accept that the author may reasonably disagree.",
    "Skip anything tooling already enforces. Formatting, import order, lint "
    "rules, and type errors are the linter's and the type checker's to report; "
    "repeating them here is noise.",
)


def _render_smell(index: int, name: str, what: str, fix: str) -> str:
    return "{0}. {1} -- what it is: {2}. How to fix: {3}.".format(
        index, name, what, fix
    )


def _rubric() -> str:
    lines = [
        "## Standards axis (hygiene)",
        "",
        "Report code hygiene on a second axis, kept separate from the spec "
        "axis. Emit every standards finding with `axis: \"standards\"`. "
        "Standards findings are advisory: they are capped at severity "
        "`WARNING` and never send a candidate back to the builder. Do not "
        "merge, rank, or trade the two axes against each other.",
        "",
        "Baseline (applies even when the repository documents nothing):",
        "",
    ]
    for index, (name, what, fix) in enumerate(STANDARDS_SMELLS, start=1):
        lines.append(_render_smell(index, name, what, fix))
    lines.extend(["", "Rules that bound this axis:", ""])
    for index, rule in enumerate(STANDARDS_RULES, start=1):
        lines.append("R{0}. {1}".format(index, rule))
    return "\n".join(lines)


#: The rendered rubric. A module-level constant so the reviewer instruction and
#: the tests read the same bytes.
STANDARDS_RUBRIC: str = _rubric()

#: Files a repository uses to document its own standards, in the order they are
#: looked for. Only paths are ever handed to the reviewer -- it reads the file
#: itself from its own checkout, so the handoff cost is one short string.
STANDARDS_FILENAMES: tuple[str, ...] = (
    "CONTEXT.md",
    "AGENTS.md",
    "CONTRIBUTING.md",
    "CLAUDE.md",
    ".eslintrc",
    ".eslintrc.js",
    ".eslintrc.json",
    "eslint.config.js",
    "eslint.config.mjs",
    "ruff.toml",
    ".ruff.toml",
    ".flake8",
    ".rubocop.yml",
    "clippy.toml",
)


def discover_standards_files(
    repo_root: Path | str, names: Sequence[str] = STANDARDS_FILENAMES
) -> tuple[str, ...]:
    """Repository-root standards files that exist, as repo-relative paths.

    Paths only. The reviewer works inside a checkout of the same tree, so it
    reads the file itself; handing over the contents would put an unbounded
    document into a handoff that is size-checked at the launch chokepoint.
    """
    root = Path(repo_root)
    found: list[str] = []
    for name in names:
        candidate = root / name
        try:
            if candidate.is_file():
                found.append(name)
        except OSError:
            continue
    return tuple(found)


def standards_section(standards_files: Sequence[str] = ()) -> str:
    """The rubric, plus the paths of any repo standards that override it."""
    if not standards_files:
        return STANDARDS_RUBRIC
    listed = ", ".join(str(path) for path in standards_files)
    return (
        "{0}\n\nThis repository documents its own standards in: {1}. Read them "
        "from your checkout and let them override the baseline above."
    ).format(STANDARDS_RUBRIC, listed)
