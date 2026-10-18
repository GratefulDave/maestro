# Deployment drift: declaring what is watched, and what the check says

The ADW runtime exists in more than one checkout. Two of them are *templates* —
this repository's `.claude/skills/sssf/templates/adws/` and the-library's
`skills/sssf/templates/adws/` — and `tests/test_template_parity.py` holds those
two together automatically, failing when they diverge.

Every other copy is a **deployment**: a repository the factory has been
installed into, running the runtime rather than shipping it. Until now nothing
watched those, and the cost was measured rather than imagined.
`lexgenius/adws/adw_modules/code_review.py` sat 639 lines behind the template
long enough to corrupt a real run's reviews — every reviewer in that deployment
judged each node against a placeholder instead of against its instruction, and
nothing in the run said so. It was found because a person went looking.

`tests/test_deployment_parity.py` is the check that looks on every suite run.
This page is how you turn it on and what it will tell you.

## Turning it on

Write one file. Nothing else is needed, and nothing is hardcoded anywhere:

```
<repository>/.maestro/deployments.json
```

```json
{
  "deployments": [
    {
      "name": "lexgenius-pipeline",
      "root": "../../lexgenius-pipeline/adws"
    },
    {
      "name": "lexgenius",
      "root": "../../lexgenius/adws",
      "note": "reconciled by hand on 2026-08-19; see issue #71"
    }
  ]
}
```

| field | meaning |
|---|---|
| `name` | what the deployment is called in every report. Unique. |
| `root` | its ADW runtime directory. Absolute, or relative to the registry file — so a registry may name a sibling checkout without naming one machine's home directory. |
| `pinned` | relative paths this deployment owns outright. Held out of the comparison in both directions, and named in every report. Optional. |
| `note` | free text, for the reader. Optional. |

`MAESTRO_DEPLOYMENT_REGISTRY` overrides the location outright when set.

Three properties of that file are deliberate:

* **A misspelled key is refused, not ignored.** A `"pinnned"` that silently read
  as "no exclusions" would put a deployment's own files back into the
  comparison; a misspelled `"root"` would watch nothing. Both are quiet, and
  quiet is the one thing this mechanism exists to remove. A registry that is
  present and malformed fails the suite rather than degrading to "no
  deployments declared".
* **A registry is looked for in the main working tree before the linked
  worktree.** A registry names roots relative to itself, and a worktree sits two
  directories below the repository, so the same file read from a lane would
  resolve `../../lexgenius/adws` to `.claude/worktrees/lexgenius/adws` — a path
  no machine has, and a skip nobody reads.
* **Absence is the ordinary case and skips visibly.** No registry, or a declared
  deployment that is not installed on this machine, skips while naming the exact
  path it looked for and how it chose it. This suite runs in CI and on machines
  that have never installed the factory. A silent skip is what hid a
  wrongly-resolved peer path for the whole life of `test_template_parity`.

## What it reports, and why the findings are separate

Each declared deployment gets three checks, because they call for three
different actions.

**A file exists in one copy and not the other.** A deletion, not an edit, and
the shape in which 6,009 lines of runtime were once lost. It has its own field
in the report so it can never be read as "some files differ".

**The template is ahead.** The deployment is running older runtime. Mirroring
repairs it and the failure prints the command.

**The deployment is ahead.** Work exists in one copy only. No command is
offered, because there is no safe one: reconciling in either direction without
reading both copies either destroys the only copy of that work or imports one
installation's local decisions into the runtime every installation ships. Files
that differ with *equal* line counts are reported here too — "we cannot show the
template is newer" is a reason for a person to look, never a licence to
overwrite.

For each deployment-ahead file, one of three answers:

1. It is newer work → it belongs upstream in the template, as its own change
   with test evidence, after which the deployment mirrors clean.
2. It is local divergence that should not survive → discard it explicitly with
   `tools/runtime_sync.py mirror … --overwrite-ahead`, having first confirmed by
   digest what is being discarded. The commit `--commit` writes names every file
   that flag discarded, so the discard is in the history rather than only in a
   terminal somebody has since closed.
3. The deployment owns it → add it to that entry's `pinned` list. It then stops
   being compared and starts being *named* in every report, instead of being
   refused on every future mirror. A refusal nobody can clear is a refusal
   people learn to ignore.

Option 3 is the only one this mechanism can express, and it is why `pinned`
exists.

## The write stays explicit

This check never mirrors, and nothing in this repository mirrors into a
deployment automatically — no hook, no post-merge action, no CI job.

A deployment is a live checkout with other people's in-flight work in it. On
2026-08-19 an agent running ordinary branch hygiene in one of them destroyed a
patch with `git restore --staged --worktree`, and the bytes survived only
because an unrelated `git add` had happened to put them in the object store
minutes earlier. An unprompted automatic write into such a repository is that
incident with a larger blast radius.

Detection is what is automatic. The command is:

```bash
# every declared deployment, on demand; reports, never writes
python3 tools/runtime_sync.py check-deployments <template> [--registry FILE]

# one pair
python3 tools/runtime_sync.py check  <template> <deployment> [--pin PATH ...]

# copy and record, in one command
python3 tools/runtime_sync.py mirror <template> <deployment> [--pin PATH ...] \
    --apply --commit
```

`check-deployments` reads the same registry the suite does and prints the same
three findings, exiting non-zero when an installed deployment has drifted. A
declared deployment that is not on this machine is reported and does not count
against that exit code.

`--pin` is the flag form of a registry entry's `pinned` list, so a failure the
check reported can be reproduced verbatim rather than approximately.

## Bringing a deployment level: one command

`--apply --commit` is the whole procedure. Drop `--commit` only for a
destination you deliberately want to leave unrecorded.

```bash
python3 tools/runtime_sync.py mirror <template> <deployment> --apply --commit
```

It copies with a sha256 assertion per file, then stages **only the paths it
wrote** and commits them. Four properties, each of which is a thing that has
gone wrong:

* **It stages by name, never `git add -A` and never the runtime directory.** A
  deployment is a live shared checkout: while this template's runtime was being
  mirrored, the-library held 24 modified and 11 untracked files elsewhere in its
  tree, none of them anything to do with the runtime. Sweeping those into a
  commit that claims to be a mirror would be its own incident. Anything the
  operator had already staged is left staged and uncommitted.
* **It refuses to copy at all if the destination holds uncommitted work in a
  file the mirror would overwrite** — whether the file is modified since its
  last commit, or has never been committed at all. That is the case where a
  mirror destroys the only copy of something, and a content comparison cannot
  see it: bytes on disk say nothing about whether those bytes were ever recorded
  anywhere. `lexgenius-pipeline` carried its entire 184-file runtime untracked,
  which is how it was rewritten with stale bytes on 2026-08-19 with no diff for
  anyone to read. The refusal names the file and asks you to commit or stash it;
  nothing is copied and nothing is committed until you do.
* **The commit message states what did *not* happen.** It names the source
  revision the bytes came from, the counts, and every file held out
  (deployment-owned or `pinned`), refused, discarded by `--overwrite-ahead`, or
  present only in the deployment. A message claiming the copies were brought
  level when eight files were held out is a false record, and records that do
  not lie are this project's entire subject.
* **It never pushes.** A local commit is recoverable; publishing one is not the
  mirror's decision.

If the destination is not inside a git working tree, the files are still
mirrored and the report says plainly that the copy could not be recorded. Its
arrival is then proved by digest and by nothing else — which is the state
`--commit` exists to get a deployment out of.

`--commit` requires `--apply`. Without it the run is a plan, and a plan has
nothing to record.
