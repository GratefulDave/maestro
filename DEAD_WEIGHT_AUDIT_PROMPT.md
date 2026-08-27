# Adversarial audit: trace every execution path, find what is not required, plan its removal

You are auditing the Maestro ADW runtime. Your job is **not** to fix bugs, add guards, or improve
anything. Your job is to find machinery that does not earn its place, prove it, and produce a
removal plan.

Repo: `/Users/davidandrews/PycharmProjects/maestro`
Runtime: `.claude/skills/sssf/templates/adws/`

## What the product is actually required to do

Four things. Nothing else is a requirement.

1. **Agent builder** — builds.
2. **Tests node** — writes tests that can actually fail, reviewed, with actionable feedback on
   failure, and it uses the last attempt rather than restarting from scratch.
3. **Reviewer** — actionable feedback on test failure, uses the last attempt.
4. **Dashboard + DAG graph.**

Every line of runtime either serves one of those four or is a candidate for deletion.

## The stance

**Guilty until proven reachable and load-bearing.** Do not ask "could this be useful?" Ask
"what breaks, on the happy path or on a real observed failure path, if I delete this today?"
If the answer is "a test fails", that is not a defence — the test may be the only thing keeping
it alive.

## The two categories you must separate

This is the whole difficulty of the task. Get it wrong in either direction and the plan is useless.

**EARNED — keep, do not propose removing.** Added in response to a real end-to-end run that
failed in a specific way. This codebase's complexity is largely scar tissue from actual
incidents, documented in `MAESTRO_architecture.md` §3.5, §3.6 and §19. Known examples:

- Falsifiability strategy and deterministic gate validation — added because tests were written
  that could not fail.
- Refusal remedies, the repeated-refusal block — added because reviewers gave feedback the
  builder could not act on, and nodes burned their whole budget on identical refusals.
- Guidance ledger, repair basis — added because the reviewer forced the builder to restart from
  scratch instead of using the last attempt.
- Cross-vendor review, located ERROR findings — §3.6 Family B.

Before proposing removal of ANYTHING, search §3.5, §3.6 and §19 for it. If an incident is
recorded, it stays, and you say so in one line and move on.

**UNEARNED — propose removal.** Machinery that was added defensively, speculatively, or in
reaction to a red unit test, with no run behind it. It is identifiable by one or more of:

- **Zero production readers.** Only tests call it. Verified examples found on 2026-08-27:
  - `scheduler_types.exits_for` / `_EXITS` — the §11.3 escape-legality table. 6 call sites, all
    in `tests/`. `_require_escape_legal` never consults `block_reason`. Production admits all 30
    (block reason × escape) combinations; 22 contradict the table. Filed as issue #155.
  - `_release_unclassified_attempt` — had **zero** callers while its docstring claimed it was
    wired to the quiescence path.
  - `launcher.until` / `until_argv` — zero readers after `agent prompt --wait` was abandoned.
  - `prompt_submission_smoke.prompt_offers` — counted a verb the runtime no longer issues.
- **A second implementation of a rule that is already implemented elsewhere**, where the two can
  and do disagree (the `_EXITS` case above).
- **A guard whose detector is itself broken or exempted.** `tests/test_no_dead_seams.py:144`
  allowlisted `exits_for` with the rationale "nothing branches on these in production" — which is
  precisely why it should have been deleted, not exempted.
- **A declaration table that must be manually kept in sync** and can therefore go stale exactly
  the way `_EXITS` did.
- **Dead branches** — a field production branches on that nothing writes, so one side never runs.

## Start here: the last three days of PRs

This is your primary audit surface. Eleven PRs merged since 2026-08-24, totalling roughly
**+49,000 / −13,600 lines**:

| PR | diff | title |
|---|---|---|
| 152 | +9910/−284 | feat(runtime): admit from transcript, amend, vault, resume floors |
| 151 | +946/−680 | docs: update architecture diagrams for amend, vault, resume |
| 150 | +476/−157 | docs: retoken HTML to maestro-dark |
| 148 | +523/−49 | fix(runtime): preserve legacy run projection |
| 147 | +5997/−73 | feat: prove gate strength for every tests node |
| 145 | +76/−61 | Show persistent review and grant resume |
| 144 | +1786/−298 | Preserve review-budget attempt on grant |
| 142 | +53/−9 | Recover replacement reviewer dispatch |
| 139 | +1374/−226 | Persist lane review lifecycle |
| 138 | **+25508/−11414** | Persist lane review lifecycles |
| 137 | +2354/−331 | Recover stalled Maestro attempts safely |

Note #138 and #139 have near-identical titles — check whether one supersedes the other and
whether both bodies of code survive.

For each PR, answer three questions and put the answers in the plan:

1. **What run failure prompted it?** Name the run id or the §19 / §3.6 entry. If none exists,
   that PR was speculative and everything it added is a removal candidate by default.
2. **Is what it added still reachable from an entry point today?** A later PR may have replaced
   the mechanism without removing it. That is the single most likely place to find dead weight
   at this volume — 49k lines in 72 hours cannot all be reachable.
3. **Did it add a guard, table, or declaration that requires manual upkeep?** List each one.

Diff a PR with `gh pr diff <n>`. Read the PR body for the stated justification, then check that
justification against §19 rather than believing it.

## Method — evidence rules, non-negotiable

- Reference counts come from `lsp_find_references`, `ast_grep`, or CBM degree fields.
  **`grep -c` / `--count` / `| wc -l` over an identifier is NEVER a reference count** and any
  finding resting on one is rejected.
- Distinguish **production readers** from **test readers** explicitly, per symbol. A symbol whose
  only callers are under `tests/` is the primary signal you are hunting.
- Beware same-name collisions. Two production dataclasses were both named `Finding`
  (`deliver.py:256`, `plan_amendment.py:99`) and a bare-name index silently attributed one's
  fields to the other. Resolve by module, not by bare name.
- Beware bare-keyword matching. A field can look "written" because an unrelated callee somewhere
  takes a keyword of the same name (issue #156). Confirm the writer is the same type.
- **Trace from entry points, not from symbols.** Start at: `maestro run start`, `run resume`,
  `retry`, `skip`, `abandon`, and the scheduler's node dispatch loop. Walk forward. Anything you
  never arrive at is your candidate list.

## Deliverable

A single markdown plan. For each candidate, one row:

| field | content |
|---|---|
| symbol / file:line | exact |
| what it claims to do | one line |
| production readers | count + where, or NONE |
| test readers | count |
| earned? | §3.5/§3.6/§19 reference, or NONE FOUND |
| what breaks if deleted | concrete |
| removal risk | LOW / MED / HIGH, with the reason |
| lines removed | approximate |

Then order them: highest lines-removed × lowest risk first. Group any that must be removed
together (deleting a table means deleting its tests and its allowlist entry, not amending them).

State the total line count the plan would remove.

## Hard constraints

- **Propose. Do not delete.** No edits to the runtime. The plan is the deliverable.
- **Do not add anything.** Not a guard, not a test, not a declaration, not a completeness check.
  Adding machinery to police machinery is the disease you are diagnosing.
- If removing something would leave a real rule unenforced, say so and say which — that is a
  finding, not a reason to build a replacement.
- Report a candidate you are unsure about as unsure. A wrong "safe to remove" costs more than an
  omission.

## What good looks like

The plan lets a human delete several hundred lines in an afternoon, in a stated order, with each
deletion independently justified by "nothing on any execution path reaches this", and with the
earned scar tissue explicitly untouched and named as such.
