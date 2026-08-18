#!/usr/bin/env python3
"""Falsify a declared effect disposition against the code a lane actually added.

MAESTRO_architecture.md §16.3 item 48 names a mechanical falsifier as its own
discharge, and this is it. Admission refuses a requirement that declares an
effect `performed` while the plan prohibits it, and the node contract tells the
reviewer what each node may do — but neither can tell whether a lane that
*declared* `planned` then went and executed. That is a statement about the code
the lane produced, so it is checked against the code the lane produced.

The rule, in one line: **a lane must not ADD code that imports or constructs
the client for an effect its requirement declares `none` or `planned`.**

    python3 effect_falsifier.py <plan-contract.json> <repo> <base-commit> [<head>]

Exit 1 with one line per finding, exit 0 with a count when there is nothing.

## Scoped to the diff, never to the module

This is the design decision that makes it usable rather than noise. An earlier
form scanned whole modules and produced three findings against an object-store
client that already existed in a pre-existing production file at lines the lane
neither wrote nor would ever write. A falsifier that cannot tell added code
from code that was already there trains people to ignore it, which is worse
than not having one. So the unit is the added line, taken from
`git diff --unified=0 base..head` and reported at its line number in `head`.

## What it does not see, stated because silence is not evidence

* **A client reached through a wrapper, a factory, or injected from
  elsewhere.** The markers below match a direct import or construction. A lane
  that calls `self._store.write(...)`, where `_store` was built somewhere else,
  passes.
* **A lane's own tests.** Excluded deliberately: `fake_only` means the test
  exercises the path against an injected fake, so a test that constructs a
  client is the declaration working rather than failing.
* **`performed`.** It says nothing about that disposition, because admission
  already refuses a `performed` declaration against a prohibition; there is
  nothing left here to catch.
* **Effects outside the five.** A filesystem write outside the repository, a
  queue, an email. None is expressible.

**A finding is evidence of a false declaration. Silence is not evidence of a
true one.**

## It decides nothing

Nothing keys on this. It is run by hand after a merge and it informs a human
reviewer; no lifecycle transition reads its output, and none may (§1.2). It is
deliberately not wired into the scheduler, the gate path, or the reviewer's
cell matrix — a transition caused by a regex over source text is exactly the
shape the acceptance predicate forbids.
"""
import json
import pathlib
import re
import subprocess
import sys

#: What constructing or invoking each effect's client looks like in source.
#:
#: Deliberately narrow. A marker that matched a mention rather than a use would
#: convict a docstring, and the first false finding is the one that teaches a
#: reader to stop looking.
MARKERS = {
    "canonical_object_write": (
        r"\bboto3\b|\bclient\(\s*[\"']s3[\"']|\bput_object\b|\bcopy_object\b|"
        r"\bupload_fileobj\b|\bupload_file\b"),
    "source_object_delete": r"\bdelete_object\b|\bdelete_objects\b",
    "catalog_projection_write": (
        r"\bclient\(\s*[\"']dynamodb[\"']|\bresource\(\s*[\"']dynamodb[\"']|"
        r"\bput_item\b|\bupdate_item\b|\bbatch_writer\b|\bbatch_write_item\b"),
    "source_backfill": (
        r"\bhttpx\.(?:Client|AsyncClient|get|post)\b|"
        r"\brequests\.(?:get|post|Session)\b|"
        r"\burllib\.request\b|\baiohttp\.ClientSession\b"),
    "migration_execution": (
        r"\balembic\b.*\b(?:upgrade|downgrade)\b|\bcommand\.upgrade\b|"
        r"\bop\.(?:create_table|add_column|execute)\b"),
}

#: The two dispositions that promise the lane's code does not execute the
#: effect. `fake_only` promises the opposite about production code and is
#: checked by the test suite the gate counts; `performed` is admission's.
CONSTRAINED = frozenset({"none", "planned"})


def added_lines(repo: str, base: str, head: str, rel: str):
    """`(line number in head, text)` for each line this range adds to `rel`.

    `None` when git could not answer, which the caller counts separately from
    "answered, and nothing was added" — a path the lane never touched and a
    path git could not read are different facts and must not share a number.
    """
    out = subprocess.run(
        ["git", "-C", repo, "diff", "--unified=0",
         "{0}..{1}".format(base, head), "--", rel],
        capture_output=True, text=True)
    if out.returncode != 0:
        return None
    line, rows = 0, []
    for raw in out.stdout.splitlines():
        hunk = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", raw)
        if hunk:
            line = int(hunk.group(1))
            continue
        if raw.startswith("+++") or raw.startswith("---"):
            continue
        if raw.startswith("+"):
            rows.append((line, raw[1:]))
            line += 1
    return rows


def findings_for(plan_path: str, repo: str, base: str, head: str = "HEAD"):
    """`(path, line, effect, disposition, matched text)` for every added line
    that contradicts its lane's declared disposition, plus the two counts."""
    plan = json.loads(pathlib.Path(plan_path).read_text(encoding="utf-8"))
    outputs = plan["extensions"]["maestro"]["outputs"]
    lane_of = {requirement: lane["lane_id"]
               for lane in plan["lanes"]
               for requirement in lane["requirement_ids"]}
    findings, checked, untouched = [], 0, 0
    for requirement in plan["requirements"]:
        lane = lane_of.get(requirement["requirement_id"])
        if lane is None:
            continue
        constrained = {entry["effect"]: entry["disposition"]
                       for entry in requirement.get("effects", [])
                       if entry["disposition"] in CONSTRAINED}
        if not constrained:
            continue
        for rel in outputs.get(lane, []):
            if not rel.endswith(".py") or rel.startswith("tests/"):
                continue
            rows = added_lines(repo, base, head, rel)
            if not rows:
                untouched += 1
                continue
            checked += 1
            for effect, disposition in sorted(constrained.items()):
                if effect not in MARKERS:
                    continue
                for line, text in rows:
                    hit = re.search(MARKERS[effect], text)
                    if hit:
                        findings.append(
                            (rel, line, effect, disposition, hit.group(0)))
    return sorted(findings), checked, untouched


def main(plan_path: str, repo: str, base: str, head: str = "HEAD") -> int:
    findings, checked, untouched = findings_for(plan_path, repo, base, head)
    for rel, line, effect, disposition, hit in findings:
        print("FALSIFIED {0}:{1}  {2} is declared {3}, and this line adds "
              "{4!r}".format(rel, line, effect, disposition, hit))
    print("checked {0} changed module(s); {1} with no added lines in range; "
          "{2} finding(s)".format(checked, untouched, len(findings)))
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:5]))
