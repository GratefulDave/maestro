# Issue sweep: build/review loop in a dedicated worktree

This document is the prompt. Hand it to a driver agent running in
`/Users/davidandrews/PycharmProjects/maestro` on `main`. The driver sets up the worktree
and the two panes, then supervises the loop; it does not write the fixes itself.

Two actors do the work, in separate Herdr panes bound to the same worktree:

| Actor | Launch | Role |
| --- | --- | --- |
| **builder** | `omp --profile grok` | Resolves issues. Never reviews its own output. |
| **reviewer** | `omp --profile openai-performance` | Reviews the merged surface. Never edits code. |

The loop is: builder fixes → writes a typed report → reviewer reads the diff and the report
→ writes a typed verdict → builder reads the verdict and fixes what FAILed → repeat.

---

## 0. Read before doing anything

`MAESTRO_architecture.md` §1 (the acceptance predicate), §1.2 (the failure predicate),
§3.6 Family B (reviewer design), and §19 (where Maestro itself broke those rules).
`AGENTS.md` for repository layout. `CLAUDE.md` for the rules that invalidate work.

Two of those rules govern this loop directly and are restated here because breaking either
means the whole sweep has to be redone:

- **§1.2** — no transition in this loop may be caused by pane text, by an agent's prose, or
  by an agent's claim about its own work. Every handoff below is a file on disk with a typed
  schema. Reading a pane to decide whether a round is finished is the defect this rule exists
  to prevent. Read panes to diagnose a stall, never to advance the loop.
- **§3.6 B12** — no actor signs off on its own output. The builder never marks its own work
  reviewed; the reviewer never edits code to make its own finding go away.

---

## 1. Create the worktree

```
herdr worktree create --cwd /Users/davidandrews/PycharmProjects/maestro --branch issues/sweep --base main --label issue-sweep --no-focus
```

Record the path it reports; every command below runs with that path as cwd. Call it `$WT`.

Create the handoff directory the loop uses:

```
mkdir -p "$WT/.sweep"
```

`.sweep/` is loop bookkeeping and must not be committed. Add it to `.git/info/exclude` in the
worktree rather than to `.gitignore`, so the sweep leaves no trace in the tracked tree.

---

## 2. Scope

Every issue open in `GratefulDave/maestro`. Get the live list rather than trusting any list
written down here — issues have been filed and closed since:

```
gh issue list --state open --limit 100 --json number,title,body
```

As of writing there are 21, and they are not one kind of work. Group them before starting:

- **Runtime defects** — #18, #20, #22, #26, #28, #29, #30, #31, #32, #34, #35, #37, #38.
  These live in the ADW runtime template, which is the only copy this sweep edits — see §4.
- **Visualizer** — #19, #27, #33, #39. These live in `.claude/skills/sssf/apps/visualizer/`,
  which exists only in this repo and has no mirror copies.
- **Copy drift** — #23, #24, #25, #21. These are about the runtime existing in four places.
  Read §19 M13 before touching them; the cost of that drift is already measured there.

Work the groups in that order. Runtime defects are what the factory ships. Do not start a
group until the previous one has a PASS verdict.

---

## 3. Launch the two panes

Builder:

```
herdr pane split --cwd "$WT" --direction right --no-focus
# note the pane id it returns, then:
herdr pane run <builder_pane> omp --profile grok --session-dir "$WT/.sweep/builder-session" @"$WT/.sweep/builder-prompt.md"
```

Reviewer:

```
herdr pane split --cwd "$WT" --direction down --no-focus
herdr pane run <reviewer_pane> omp --profile openai-performance --session-dir "$WT/.sweep/reviewer-session" @"$WT/.sweep/reviewer-prompt.md"
```

The trailing `@<path>` is omp's `MESSAGES` positional and is how the prompt is delivered.
Do **not** type the prompt into the composer afterwards — that stalls roughly half the time,
the text lands and is never submitted, and `run-d7c242809fe74e74b7368393fa4de6de` is what it
costs (both lanes blocked at 0 turns after four submit attempts each).

**Close every pane when its work is done**: `herdr pane close <pane_id>`, positional. `--pane`
fails. Reviewer panes are not named `maestro-*`; identify them by agent kind, cwd, and title.

---

## 4. Rules the builder must follow

These are not style preferences. Each one has a production failure behind it.

**The ADW runtime exists in three copies. This sweep edits exactly one of them.**

| Copy | Role |
| --- | --- |
| `.claude/skills/sssf/templates/adws/` | The template. Edit **only** here, inside `$WT`. |
| `the-library/skills/sssf/templates/adws/` | Install source. Live shared checkout. Never write to it. |
| `lexgenius-pipeline/adws/` | Deployed instance. Live shared checkout. Never write to it. |

**The sweep never mirrors.** Mirroring happens exactly once, at landing time, performed by the
operator in the main-thread session, after review has passed. The builder edits the template
inside its own worktree and stops there.

`the-library` and `lexgenius-pipeline` are live shared checkouts that other agents are working
in concurrently. They are not worktrees of this sweep. Writing into them from here defeats the
worktree isolation the sweep was given: the edits land on whatever branch those repos happen to
have checked out, collide with other agents' work, and leave those repos dirty on a shared
branch. That has already happened once, and the writes had to be reverted.

Never hand-edit any copy other than the template. Never use `git apply` — it silently no-ops in
this environment and reports success.

Never copy `maestro.config.yaml` between copies. It is deployment-specific by design.

**The parity tests are expected to fail inside this worktree.**
`tests/test_template_parity.py` here, and `tests/test_sssf_adws_copy_parity.py` in
`the-library`, compare the template against the-library's copy. Editing the template without
mirroring makes them red by construction, and they stay red until the operator mirrors at
landing time. A red parity test is not a finding for this sweep, is not a blocker, and must
never be "fixed" by copying files into another repository. Do not run them and do not report
them.

**Environment hazards, all of which have cost time here:**

- `rm` is aliased to `trash` and corrupts filenames. Delete with python `os.remove` /
  `shutil.rmtree`.
- System `python3` has no pytest. Use
  `/Users/davidandrews/PycharmProjects/lexgenius-pipeline/.venv/bin/python`.
- `git diff` is heavily body-stripped by RTK. Use `rtk proxy git diff` before any claim
  about what a diff contains.
- A repo whose `pytest.ini` sets `-v` cancels `-q`, so every collection count must pass
  `-o addopts=` or it silently returns zero.
- Do **not** `git checkout main` in `lexgenius-pipeline`. It sits on
  `parked/cmo-consolidation-l-run` and `_run_start` refuses `INTEGRATION_BRANCH_CHECKED_OUT`.
  Read main's bytes with `git show main:<path>`.
- Never `git clean -xdf` in `lexgenius-pipeline`: `.maestro/` is gitignored and holds the plan
  IR, the receipts, and the whole `adws/` runtime.

**Evidence rules:**

- A fix is not done until a test fails before it and passes after it. Prove the fail-before by
  reverting the change and running the test, then restore by sha256. Report both counts.
- A passing suite is not evidence until you have shown the tests execute the production code
  path. Tests that reimplement the logic they claim to cover prove nothing.
- Placeholder notes, `test.skip`, `test.only`, stub tests, and unimplemented branches are
  blockers, not progress. Report them as blockers rather than leaving them.
- Full template suite: `cd <template adws> && <venv>/python -m pytest tests/ -o addopts=
  -p no:cacheprovider -q`. Roughly 19 minutes. Baseline to beat: 1551 passed, 0 failed.

**Defect class, not defect instance.** On finding a bug, state its shape in one line, then
find every other instance of that shape on the affected path *before* proposing a fix. Report
the full count first. Fixing one instance while its siblings remain is a failed task. §19 M14
is what this rule is made of: one selector bug had three instances and only one was visible.

**Documentation is part of the fix**, not a follow-up. A defect with a lesson goes in
`MAESTRO_architecture.md` §19 with its measurements; something deferred or left unenforced goes
in §16.3. User-visible changes go in `CHANGELOG.md` under Keep a Changelog format.

---

## 5. The handoff contract

Both directions are files. Neither actor reads the other's pane to decide anything.

### Builder → reviewer: `.sweep/round-<N>/build-report.json`

```json
{
  "round": 1,
  "group": "runtime",
  "issues": [
    {
      "number": 20,
      "shape": "one-line statement of the defect class",
      "instances_found": 3,
      "instances_fixed": 3,
      "files": ["adw_modules/watchdog.py"],
      "tests": ["tests/test_watchdog.py::test_process_dead_unreachable"],
      "fail_before": "6 failed",
      "pass_after": "1551 passed, 0 failed",
      "architecture": "§19 M16",
      "status": "fixed"
    }
  ],
  "blocked": [],
  "base_sha": "<sha the round started from>",
  "head_sha": "<sha the round ended at>",
  "suite": {"passed": 1551, "failed": 0, "subtests": 165}
}
```

Write it **atomically** — write `build-report.json.tmp`, then rename. A reviewer that reads a
half-written report reviews a workflow that does not exist.

An issue the builder could not fix goes in `blocked` with the reason, never silently dropped
and never marked `fixed`. Scaling the work down is the operator's call, not the builder's.

### Reviewer → builder: `.sweep/round-<N>/verdict.json`

```json
{
  "round": 1,
  "verdict": "FAIL",
  "reviewed_sha": "<head_sha from the build report>",
  "findings": [
    {
      "severity": "ERROR",
      "file": ".claude/skills/sssf/templates/adws/adw_modules/watchdog.py",
      "line": 331,
      "issue": 20,
      "finding": "what is wrong",
      "why": "why it is wrong, citing the architecture section or the test that proves it"
    }
  ]
}
```

**`FAIL` is structurally impossible without at least one finding of severity `ERROR` carrying
a `file` and a `line`.** A verdict that fails without a located error finding is malformed and
the driver rejects it rather than acting on it (§3.6 B8 — a field added later is optional
forever, so this is enforced from the first round).

`WARNING` and `NOTE` findings do not block. They are recorded and carried forward.

The reviewer's input is a declared contract: the issue text, the build report, and the diff
`base_sha..head_sha`. It reviews the merged surface, not the builder's description of it.

**Size-check before dispatch.** Measure the diff plus the report against the reviewer's
context window before handing it over, and fail closed if it does not fit. An overflowing
reviewer does not error — it fabricates a verdict about a different workflow (§3.6 B13).
If a round is too large to review, split it by issue group and review each part.

---

## 6. The loop

```
round = 1
group = first unfinished group
loop:
    builder works the group, writes .sweep/round-<round>/build-report.json
    driver waits for that file to exist and to parse
    driver size-checks diff + report; splits the round if it does not fit
    driver hands the reviewer: issue text, build report path, base_sha..head_sha
    reviewer writes .sweep/round-<round>/verdict.json
    driver waits for that file to exist and to parse

    if verdict == PASS:
        group is done; commit; advance to the next group; round = 1
    if verdict == FAIL:
        round += 1
        builder reads verdict.json and fixes every ERROR finding
        a finding the builder disputes is answered in the next build report
        with evidence, not deleted
```

**Termination.** Stop and report — do not start another round — when any of these is true:

- Three consecutive FAIL verdicts on the same group without the ERROR count falling. That is
  not convergence, and another round will not produce it.
- Six rounds on one group, whatever the trend.
- Any round where the suite count drops below the 1551 baseline and the builder cannot say why.
- A reviewer verdict that is malformed, or that reviews a sha the build report did not name.

On any of those: stop, write `.sweep/HALTED.md` naming the group, the round, the surviving
findings, and what was already landed. Carve the failing issues into deferred rows, land what
passed, and hand back. Never launch more work to fix a non-converging signal.

**Do not gate on a zero-finding sweep with restart-on-any-finding.** It has no bounded
termination (§3.6 A9). The bounds above are what replaces it.

---

## 7. Landing

One branch, `issues/sweep`. Commit per issue group, not per issue and not one commit for
everything. Each commit message names the issues it closes with `Closes #N`.

Commit messages, PR bodies, code comments and documentation are written in normal professional
prose. No `Co-Authored-By:` trailer. Keep the `Claude-Session:` URL trailer.

Open one PR per group. After merge, delete the branch both remote and local — prefer
`gh pr merge --squash --delete-branch`.

Anything deferred, unresolved, or discovered along the way gets a **GitHub issue**, not a code
comment and not a line in a report that nobody reads.

Close every Herdr pane when its work is done. Remove the worktree when the sweep is finished:
`herdr worktree remove`.
