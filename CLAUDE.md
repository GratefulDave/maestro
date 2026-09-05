# maestro — read this before doing anything

Maestro is a **nine-stage artifact factory**. Durable workflow authority is the one
mutable `lane_state.stage` field (`PLANNED`, `WRITING_TESTS`, `REVIEWING_TESTS`,
`TESTS_SEALED`, `BUILDING`, `REVIEWING_CODE`, `READY_TO_MERGE`, `MERGED`,
`WAITING_FOR_USER`). Git commits and sealed artifact digests identify immutable
inputs and outputs; they do not independently encode stage. Ledger, vault, locks,
receipts, copied plans, and ephemeral worktrees live only under the deployment's
absolute `runtime_state_root` (mode `0700`, outside the target repository). Every
`run start`, `run resume`, `run amend`, and `run status` revalidates
`runtime_state_fingerprint`. Operator execution is only
`uv run adws/maestro.py run start|resume|amend|status` from the stamped `adws/`
copy against a bound `--repo` publication worktree. Template-source run creation
refuses `RUN_REPOSITORY_MISMATCH`. Herdr and OMP are transport for agent dispatch;
pane text, process liveness, and session directories are not workflow authority.
There is no retry, skip, abandon, attempt-salvage, or coordinator/workspace verb.

## Mandatory reading, before writing any code or spawning any agent

| Document | What it binds |
|---|---|
| `MAESTRO_architecture.md` | Executable factory contract: nine stages, compiler checks, artifacts, runtime binding, private-test boundary, operator verbs |
| `docs/plan-authoring.md` | The only supported path from a source document to an executable plan |
| `AGENTS.md` | Repository layout and where the implementation actually lives |

Do not design a feature this project has already specified. Check these first; if a brief
you were given contradicts them, the documents win — say so rather than following the brief.

## Three rules that invalidate work if broken

1. **Typed records only** — a run fails if any lifecycle transition is caused by
   pane text, prompt text, a free-text envelope field, or an agent's claim about
   its own work. Transitions key on immutable artifacts and `PASS` / `REVISE`
   verdicts, never on model prose.
2. **Sealed tests stay private** — accepted tests are sealed in the vault. The
   builder receives the public contract, architecture constraints, allowed paths,
   prior redacted review, and sealed digest. It does not receive private source,
   fixtures, selectors, expected literals, or vault paths.
3. **A weakened check is a contract change, never a fix** — a diff that deletes
   or weakens a verdict, a check, or an error path is never the fix for a
   blocked run. It lands on its own, labeled as such, separately from the bug
   that provoked it.

Rule 3 has two receipts in this repository, and both were bundled.

`ad186ba` (#187, 2026-09-02), "stop refusing work the factory has already
accepted", carried seven changes under a theme of unblocking a stuck run. One of
them, in `review_builder_output` (`adw_modules/code_review.py`), rewrites a
reviewer's `REVISE` to `PASS` whenever the sealed suite is green and moves the
findings to an `advisory_findings` key that never reaches the builder and never
causes a transition. The scheduler logs it plainly — `"sealed suite is
authoritative; verdict recorded as PASS"` beside `"reviewer said REVISE"`
(`adw_modules/scheduler.py`). Bundled with six other changes, nobody saw it.
Alone, it would have read as what it is: a decision that a reviewer cannot block.

`c2cece7` (#205, 2026-09-04) added `FactoryScheduler._drop_unobservable_findings`.
The integration reviewer kept reporting that sealed test paths were absent from
its checkout. The finding was wrong as a gate failure and correct as a fact about
the branch — the tests genuinely were not in the repository. Instead of releasing
them, the fix dropped the finding and turned a `REVISE` with no survivors into
`PASS`. It shipped bundled with an unrelated envelope-race fix, and it persists
nothing about what it dropped: the artifact keeps only the survivors and the log
keeps a count, so the evidence a suppression destroyed cannot be recovered
afterwards.

What that cost: FDAdb run `a33d5e9b` published `refs/heads/integration` =
`5c43e4fbc058` with two defects its own reviewer had named correctly, with
locations, in four consecutive rounds. Both were later reproduced by running them
against the published file:

```
spl_bytes("../../secret") -> b'TOP SECRET'      # path traversal: no hex validation, no containment check
24 threads, one key      -> 2 rows appended     # append-only store: _rows() scan then open(...,"a") under no lock
```

`agree=False` printed in the lane gate table on all four rounds and was read by
nobody.

## Where the implementation lives

The ADW runtime exists in three copies:

| Copy | Role |
| --- | --- |
| `.claude/skills/sssf/templates/adws/` (this repo) | The template. Where the factory ships from. |
| `lexgenius/adws/` | A deployed instance, and in practice where fixes have landed first. |
| `the-library/skills/sssf/templates/adws/` | The install source for `skills/sssf`. |

**There is now one script that copies one to another, and it is the only one you may use.**
`.claude/skills/sssf/templates/adws/tools/runtime_sync.py` — `check <source> <destination>`
answers "are these level?" structurally (which files differ and in which direction, and,
separately, which exist in one copy and not the other, because that is a deletion rather than
an edit); `mirror <source> <destination>` plans and `--apply` writes, with a sha256 assertion
per file. It never deletes, it refuses a destination that looks ahead of the source unless you
pass `--overwrite-ahead`, and it holds `maestro.config.yaml` back whenever either endpoint is
a deployment. `tests/test_template_parity.py` calls the same comparison, so the test's failure
and the mirror's repair are the same definition of level. Do not use `cp`, `rsync`, or
`git apply` — `git apply` reports success and changes nothing on this machine.

Before it existed there was nothing, and the drift was only discovered when something broke: on
2026-08-17 the template copy was found ~750 lines behind in `maestro.py` alone, missing every
plan verb in daily use, and a revert in a consuming repo silently deleted 6009 lines of runtime
from another copy. Nothing *schedules* the new script either. It makes a reconciliation provable
and refusable; it does not make one happen.

The two **template** copies are now held together by a test rather than by discipline:
`.claude/skills/sssf/templates/adws/tests/test_template_parity.py` compares this repo's template
against the-library's file by file and fails naming the files that differ and in which direction.
It is mirrored into the-library along with the runtime, and re-exported into that repo's own
suite by `the-library/tests/test_sssf_adws_copy_parity.py`, so both repositories check the
invariant from their own side. A file present in one copy and not the other fails, which is the
6009-line loss mode. It skips only when the peer repository is not checked out at all; a peer that
is checked out with its runtime directory missing fails.

`lexgenius/adws/` is **not** covered by that test. A deployed instance carries its own
`maestro.config.yaml` and legitimately runs ahead of the template, so it is reconciled by hand.
Treat this repo's template as authoritative for what the factory *ships*, but **verify before
assuming it is current against a deployment** — run `runtime_sync.py check` against the instance
rather than trusting this file, which can itself go stale. When landing a change, say explicitly which copies
you touched, and mirror deliberately rather than assuming a mirror already happened.

**Historical snapshot as of 2026-08-22** (attempt-host / retry-policy PRs named below
are not factory diagnostics; the parity counts are a dated observation):
`tests/test_template_parity.py` **passes** from this repo — 2 passed in 0.32s,
both assertions, same-files and byte-identical-contents — and
`runtime_sync.py check` agrees: template and the-library are level over **214**
compared files. That is after #127 (console stops reading an in-review attempt
as dead), #128 (`attempts.attempt_host` + `attempt_start_epoch`; attempt
liveness declinable; `tester_vendor` bound to `lane_vendor`), and #130
(per-lane `PYTEST_ADDOPTS` worker cap; `reviewer.turn_timeout_s` raised) landed
on maestro `main` at `f89e0bf`, and the-library was brought level by
`runtime_sync.py mirror --apply` in its #128 (`46111a9`) and #129 (`5400993`).
Earlier readings recorded here (189, then 200, then 201 files) are superseded
rather than wrong: the count moves whenever a test file is added, which is
exactly why it dates a snapshot instead of describing an invariant.

Two qualifications outrank any such snapshot, this one included. **the-library's
mirror can be uncommitted there.** It is committed as of 2026-08-22 — 214
tracked files under `skills/sssf/templates/adws/`, nothing modified or untracked
under that path, and `main` at `5400993` equal to `origin/main` — but it has
been uncommitted before, and the check that would catch it is not the parity
test. A green parity run or a `runtime_sync check` says the bytes on disk
agree; it says nothing about whether they are committed, and an agent that
resets or checks out in that repository takes an uncommitted mirror with it.
Confirm with `git status` there, not with parity. That repository also carries
unrelated work — 5 modified files and 1 untracked path elsewhere in its tree
(`library.yaml`, three plan-contract/plan-brownfield files, and
`skills/install-anti-slop/`). Stage only the paths the mirror wrote —
`git add skills/sssf/templates/adws` — which is what preserves them; a
`git add -A` in that repo would sweep them into a commit claiming to be a
mirror. And the differing set moves while it is being measured. Any comparison
between these copies is a reading of a moving tree — run the test, do not
trust this paragraph.

Both deployments **track** `adws/` in git — `lexgenius` at 213 files on
`main`, `lexgenius-pipeline` at 209 on `save/repair-chain-falsifiability`,
each still under a commit named "Mirror the ADW runtime from maestro into this
deployment". Tracking is not levelness. The 2026-08-20 reading that recorded
those copies as level is superseded by the 2026-08-22 check below.

**Historical snapshot re-derived 2026-08-27** with `runtime_sync.py check` (not
current factory diagnostics; identical-refusal retry/block is withdrawn authority):
not `diff -rq`, which reports a filename without saying which side is ahead.
The counts moved because two test files were added that day, which is exactly why
a count dates a snapshot rather than describing an invariant:

| comparison | result |
| --- | --- |
| template ↔ the-library | level over **231** files |
| template ↔ `lexgenius-pipeline/adws/` | level over **230** files |
| template ↔ `lexgenius/adws/` | level except **three deployment-owned docs where lexgenius is AHEAD** — `tests/AGENTS.md` (+15 lines), `tests/fixtures/AGENTS.md` (+7), `tests/fixtures/step8/AGENTS.md` (+9). They are pinned, deliberately, and must stay pinned: a mirror that discards them is destroying the deployment's own documentation. |
| template ↔ `lexgenius-pipeline-epa-national-corpus/adws/` | **not level**: `tests/test_refusal_remedies.py` and `tests/test_repeated_refusal.py` absent, and 6 modules behind — `launcher.py` (+273 lines), `retry_policy.py` (+181), `scheduler.py` (+160), `route_admission.py` (+65), `scheduler_types.py` (+12), `code_review.py` (+2). |
| template ↔ `.worktrees/fdadb/integration/adws/` | **not level**: identical shape to EPA. |

Both live deployments are deliberately behind as of this reading. The template
gained the repeated-refusal block (§19.6 M49) and the refusal-remedy obligation
(M50) that day, and mirroring those changes **how a run terminates** — a node
that refuses identically twice now blocks rather than retrying. That is a
decision about a running factory, not a housekeeping sync, and it was left to
the operator rather than taken by the agent that made the change.

**Historical snapshot re-derived 2026-09-02** with `runtime_sync.py check`,
template ↔ the-library only: a run was executing against FDAdb, so no deployment
was read.

| comparison | result |
| --- | --- |
| template ↔ the-library | level over **155** compared files at this reading, but only after the repair below, and that repair was still uncommitted in the-library when this was written. Nothing is excluded and no file is missing from either side. |

How that one file reads wrong is the part worth keeping. The check reports
the-library "ahead by 141 lines (343 vs 202)" — and the-library is not ahead.
The template's copy is the newer one: #175 deliberately dropped the
delegation-capability axis from role launch, deleting `route_capability_argv`,
`argv_denies_delegation` and the `--disallowedTools Task Agent` flags they
produced. the-library still holds the pre-#175 file from the 2026-08-21 mirror,
so **the newer side reads as behind purely because its file got shorter.**

This is the one shape `--overwrite-ahead` is for, and a plain
`mirror --apply` will not fix it: the mirror refuses a destination file that
looks ahead, so every ordinary sync carries the other 154 files across and
silently leaves this one diverged. It survived two such syncs that way, including
`sync(sssf): mirror the ADW runtime from maestro (#151)`, whose commit message
claims a mirror this file was not part of.

The flag's danger is unchanged everywhere else — read which side is ahead, and
why, before reaching for it. And **the repair is not durable until it is
committed in the destination repository**, which is a separate act from writing
it. That is not hypothetical here: on 2026-09-02 the overwrite was applied, a
check confirmed the copies level, and a later git operation in that repository
discarded the uncommitted file and put the divergence straight back. A green
`test_template_parity` proves the bytes on disk agree; it says nothing about
whether they are committed, so confirm with `git status` there, never with
parity.

The compared set is 155 where 2026-08-27 read 231, and `excluded` and
`missing_files` are both empty, so that is a runtime with fewer files in both
copies rather than a widening exclusion.

`--overwrite-ahead` deserves its own warning here, because it was nearly used
on the `lexgenius` docs above. The flag discards destination files that look
*ahead* of the source — which is indistinguishable from someone's uncommitted
work, or from a deployment that legitimately owns a richer file. Run `check`
first, read which side is ahead, and treat an ahead file as a stop-and-report
condition. A refusal from the tool is the tool working.

The 2026-08-20 episode that brought all three level is history, not the current
state. Issue #71's per-file question was answered in that episode — the
deployment-ahead files were read and, where the template was a strict
superset, overwritten with `--overwrite-ahead` after reading the line. That
flag's only use remains that one. The copies have since drifted again, which
is what §16.3 item 50 said would happen: nothing schedules a reconciliation.

Mirroring a deployment still costs what #104 added. A `maestro-plan.v1` plan is
**unrunnable** at run start (`RUN_PLAN_SCHEMA_VERSION_UNRUNNABLE`); the remedy
is to re-ship the plan from its IR, which is cheap but is not nothing, and it
cannot be done while that plan is mid-run. Mirroring this template into a
deployment that holds shipped v1 plans refuses those plans until they are
re-shipped, so sequence the mirror against what is running there rather than
treating it as a neutral copy.

**The `_node_goal` check below is withdrawn as of 2026-09-05 — it now answers
`0` everywhere, including in this repository's own template, and that is
correct rather than alarming.** The artifact-factory cutover (`e7b477e`) deleted
the function; a reviewer's contract is now projected from
`public_contract["acceptance_criteria"]` (`adw_modules/code_review.py`). Anyone
running the grep on a current deployment reads a `0` that used to mean "every
review here is degraded" and means nothing at all today. There is no one-line
successor: to check that a deployment's reviewers hold a real contract, read a
recent `CODE_REVIEW` artifact and see whether its findings cite the lane's
acceptance criteria. The 2026-08-18 hand-level of
lexgenius-pipeline remains the episode that cost
`run-0120c32064d144c2aa55c344087e0b0a`, whose every reviewer was told "Make
the gate '…' pass over selector '…', changing only the declared outputs"
verbatim while the plan it was running carried the correct instruction
(§19 M13).

Nothing enforces any of this and nothing would notice it drifting again (§16.3 item 50). Do not read
"level" as a state anything maintains, and do not read a differing filename as a cost — `code_review.py`
was reported as differing the whole time it was silently degrading every review in that deployment.
Re-derive the table above before relying on it; it is a dated observation, not an invariant, and the
only invariant here is the parity test between the two template copies. There used to be a one-line
check here for the divergence with a known behavioural cost:

```bash
grep -c "def _node_goal" <deployment>/adws/adw_modules/code_review.py   # WITHDRAWN — answers 0 everywhere; see above
```

It is kept only so nobody reintroduces it from memory. A cheap check that outlives the code it
greps for is worse than no check: it reports a catastrophe on a healthy deployment, and the next
reader either panics or learns to ignore the file.

`maestro.config.yaml` is deliberately *not* copied from a deployment. It is deployment-specific:
its lane vendors, models, and concurrency name a particular installation. The template's copy
carries the same schema keys as the deployments, with template-shaped values, and the parity test
does compare it between the two template copies.

That hold-out is why mirroring the runtime never makes a deployment run lanes in parallel.
`concurrency` is opt-in: the template ships `1`, absent means `1`, and `runtime_sync` holds
`maestro.config.yaml` back, so an upgraded install stays serial until someone sets the key in
*that deployment's* file. FDAdb is set to `3`; the other deployments are unset and therefore
serial. A deployment's value also survives every later mirror, which is the point — do not
"fix" a deployment that looks different from the template here.

The visualizer (`.claude/skills/sssf/apps/visualizer/`) exists only in this repo — no copies,
no ambiguity.

## Historical — reviewer design incidents (not current factory diagnostics)

`MAESTRO_architecture.md` §3.6 (Family B) records these as observed production failures:

- No actor signs off on its own output; review is cross-vendor over the merged surface (B12).
- A verdict carries structured findings bound to locations, and FAIL is structurally
  impossible without a located ERROR finding — **enforced from v1**, because a field added
  later is optional forever (B8).
- The reviewer's input is a declared contract: goal, `produces`, acceptance (B9).
- Size-check every handoff against the reviewer's context window before dispatch and fail
  closed; an overflowing reviewer fabricates a verdict about a different workflow (B13).
- Re-review of byte-identical input is impossible or explicitly recorded, with an operator
  escape (B10).
- Detect quiescence-after-liveness rather than imposing a wall clock, and arm detection only
  after `working` or `blocked` is first observed (B14).
- Never gate progress on a zero-finding LLM sweep with restart-on-any-finding — it has no
  bounded termination. Bound the loop or accept graded findings (A9).
- If a check's field has zero readers, that is a build failure (B15).
- **A tests node is reviewed like any other producing lane, and by its own rubric.** The
  derived review edge covers `agent` and `tests`; a code node still has none, because its
  acceptance is its command's exit code. A test reviewer is asked whether the cases
  discharge the declared obligations, exercise real boundaries, and would fail a plausible
  wrong implementation — never "does this pass the gate on the merits", which is the wrong
  question about a diff whose purpose is to be red (§19 M41).
- **A tests node's acceptance is measured, not counted.** The plan declares which case ids
  discharge which requirement and aspect, and code counts them; the declared falsifiability
  strategy is executed and its failure must match the declared reason. An implementation
  then binds to the exact accepted test sha. A case count, a green command, and a valid test
  file are each compatible with a suite that asserts nothing.

§19 records where Maestro itself broke these. B9's declared contract was degenerate in
production until 2026-08-18 — a projection silently dropped the node's `instruction`, so every
agent-node reviewer was told "make the gate pass" and nothing else (§19 M1) — and B13's size
check sat on one launch path instead of the chokepoint every route crosses (§19 M6), and the
derived review edge covered agent nodes only, so a `tests` node reached MERGED with no
independent reader at all (§19 M41). A lesson in the list above is not a property of the code;
read §19 beside it.

## Historical — per-node Gate.runner / min_cases (withdrawn as current-facing)

`Gate.runner` was `Literal["pytest", "vitest"]` because Maestro counted executed
cases against `min_cases`. A shell script, Makefile target, `psql` migration,
or `curl` check proved nothing countable and was refused with `maestro.command`.
Factory code review still runs sealed pytest against a candidate; that runner is
not a lane-stage gate and `min_cases` is not workflow authority. Counting note
that remains true for any pytest invocation: a repo whose `pytest.ini` sets `-v`
cancels `-q`, so collection must pass `-o addopts=` or it silently returns zero.

## Herdr panes (transport only)

Close every pane when its work is done — `herdr pane close <pane_id>`, positional; `--pane`
fails. `kill_reviewer` does not reliably close panes. Reviewer panes are not named
`maestro-*`; identify them by agent kind, repo cwd, and title. Pane liveness is not
lane stage.

**A Space's repository binding is `herdr worktree list`, never `WorkspaceInfo.worktree`.**
Herdr fills that record field in only for a Space it binds when it creates it — a
`workspace create --cwd <repo>` on a repository that has no source Space yet, or a
`worktree open` — and it never backfills. So every Space an operator opened reports no
`worktree` at all, while `worktree list --cwd <repo>` simultaneously names one of them
`source.source_workspace_id`. A repository has exactly one source Space: the first opened
on its primary checkout. A second `workspace create --cwd <same repo>` is handed no
binding and does not displace it. Closing a Space cascade-closes its linked children.

That field cost two shipped bugs. Believing it made every operator Space read as unbound,
so a run created a second Space, which Herdr then also left unbound, and refused itself
with `WORKSPACE_UNRESOLVED:RUN_WORKSPACE_UNBOUND:<id>:NO_WORKTREE_BINDING`. The suite was
green throughout because `tests/herdr_fake.py` synthesized the field on every record.
**A fake built from `herdr api schema --json` proves only that a field is permitted, never
that the binary sends it.** Before depending on any Herdr field, read it off the real
binary — `herdr workspace get <id>`, `herdr worktree list --cwd <repo>` — in a throwaway
Space, and delete the Space afterwards.

## Historical — 2026-08-29 attempt/repair cluster (not current diagnostics)

A cluster of refusals on one path is usually **one wrong idea wearing different
faces**. Name the idea, make the smallest change that states it correctly, and let
the symptoms fall together. Patching each refusal as it appears produces a sequence
of confident "it's fixed, run it" handovers that each die at the next gate, and it
grows the code by one special case per symptom.

On 2026-08-29 `lane-wp6-build` produced four distinct refusals in a row —
`HeadMoved: HEAD is 3b1f1beb70, not candidate parent a551049c94`,
`late envelope is not usable`,
`candidate replay disagrees with the immutable publication`, and
`AttemptOwnershipLost: builder generation changed`. They were one idea:

> **A repair is a property of the candidate, not of the attempt that happens to be
> running.** The candidate owns the commit a repair must descend from, the
> publication identity it must reassert, and the builder generation it must be
> delivered to. `_resume_unreviewed_candidate` reviews a candidate an *earlier*
> attempt built, so every one of those read off the running attempt instead.

The operational half of the same rule: **walk the whole path and enumerate every
gate on it, with its actual value from the ledger and from git, before writing a fix
and before handing the operator a `run start` / `run resume` line.** A path has more
gates than it has bugs, and the first sufficient explanation is not the explanation.
For this path the gates were, in order: node state and block reason; the latest
attempt's `extra_json` recovery keys; `attempt_sealed_output`;
`lane_candidates.builder_generation` and `parent_candidate_sha`;
`repair_handoffs.builder_generation` and state; `candidate_reviews.state`,
`verdict`, and `reviewer_generation`; `current_actor_session` and the set of
`actor_sessions` generations for both roles; `IDENTICAL_REFUSAL_LIMIT` against the
stored refusals; then the worktree HEAD and `refs/heads/maestro/{run}/{node}/a{n}`.
Four of those were wrong at once. Reading three of them and stopping is what cost
four handovers.

## Historical — 2026-09-02 run f50638ab (not current diagnostics)

Two failures on one FDAdb run, unrelated in mechanism and the same in shape: a
value was read that answered a different question than the caller was asking,
and neither the type nor the query said so.

**A slow composer is not a verdict about work already declared.**
`wait_for_interactive_agent` raised a bare `RuntimeError`, so no caller could
separate "this agent refused" from "this pane has not finished drawing", and no
caller caught it. One of those callers is `_await_envelope`, which runs *after*
the envelope is written. On run f50638ab the tester wrote a valid envelope,
`_await_envelope` read it, validated it and satisfied `_payload_ok` — and then
the 60s courtesy wait for the Claude pane's composer timed out with
`AGENT_INTERACTIVE_READY_TIMEOUT` and ended the run while holding the good
result. The agent read `idle` / `interactive_ready` moments later. The fix
decides nothing new: `AgentNotInteractive` is the same failure, typed.
Post-envelope it is reported and the payload returned, because the correction
path re-checks composer readiness itself before it submits. At `resubmit`, where
a prompt genuinely cannot be delivered into a busy composer, it becomes
`LaunchRefusal.PROMPT_SUBMISSION_REFUSED`; at session cleanup,
`SESSION_RENAME_UNCONFIRMED`. **An untyped exception on a path with more than one
caller defers the decision to whichever caller forgets to catch it**, and a wait
whose own docstring says the work is already on disk must not be able to discard
it.

**A final review an amendment answered is spent.** `_active_final_review` took
the newest `FINAL_INTEGRATION_REVIEW` row with no freshness test of any kind, and
`apply_amendment` reads it to decide which lanes an amendment must change. On the
same run a REVISE named `lane-wp7-build` and `lane-wp7-gateway-build`, the v3
amendment answered it, and both lanes reached `MERGED` — after which every later
amendment was refused `AMENDMENT_DOES_NOT_ADDRESS_REVIEW` for not re-editing two
lanes that were merged and correct. The only way to satisfy that refusal was to
damage finished work. A `PLAN_AMENDMENT` recorded after the review *is* the
answer, since applying one is the only way to respond to a REVISE, so the lookup
now ignores a review a later amendment precedes. **Everywhere else a final review
is bound to the fingerprint of the surface it judged; this one lookup was bound to
nothing.** Where a stored record authorizes something, find the query that fetches
it and ask what makes it current — `ORDER BY sequence DESC LIMIT 1` is a guess
that the newest row is still the live one.

## Historical — diagnosing attempt-worktree retries (not current diagnostics)

A node that retries forever is almost never the agent's fault. Before writing any fix,
**execute the harness's own measurement by hand, in the attempt worktree, under the real
binary.** The worktrees survive the run at
`~/.maestro/<install>/runs/<run_id>/worktrees/<run_id>-<node_id>-a<N>/`, provisioned, with
`node_modules` in place. Reproducing a refusal there costs seconds.

Two incidents on 2026-08-27/28, both on `lane-wp6-tests`, both producing the byte-identical
verdict `TESTS_NO_NEW_CASES: no new collected case versus the parent commit`, and each one
alone sufficient to explain it:

1. `_prove_tests_red_at_parent` did `del node` and collected with `_pytest_prefix()`
   whatever the gate declared. pytest collects nothing from a `.test.ts` file, so a vitest
   node measured zero on every attempt. Fixed by dispatching on `node.gate_command[0]`
   (`407d7d3`).
2. `VitestCaseRunner.collect` built `vitest list --json <paths>`. vitest's `--json` takes an
   *optional value*, so the path was read as "write the listing here": collection
   **overwrote the tester's committed test file with 47KB of vitest's own JSON**, printed
   nothing, exited 0. Fixed by putting the filters before the flag (`5273342`).

Bug 1 was shipped as the whole answer and the run failed again identically. The lesson is
the ordering rule, not the two bugs: **the first sufficient explanation is not the
explanation.** A refusal string identifies a measurement that returned zero; it does not say
why, and two independent causes can produce the same string. Run the measurement under every
runner the plan can name before concluding.

Consequences that follow from this, and that the incidents paid for:

- **A green suite proves nothing about a code path no test executes.** `VitestCaseRunner`
  had no test at all — 2943 passing tests, and not one had ever called `collect()`. Check
  with `grep -l <runner-or-subject> tests/*.py` *before* trusting the suite about it.
- **A stubbed `subprocess.run` cannot observe an argv parser.** It records the argv you
  passed and returns the stdout you scripted. Any claim about how an external tool *reads*
  its arguments must be tested by running that tool. `tests/test_vitest_collect_argv.py`
  installs vitest and compares the test file's bytes before and after collection; that is
  the only case that could have caught bug 2.
- **A measurement must never mutate its subject.** Bug 2 destroyed the evidence it was
  measuring, then blamed the tester for its absence. Any harness command aimed at a
  candidate's own files is suspect until proven read-only by byte comparison.
- **A patch on disk does not reach a running scheduler.** Python binds modules at import, so
  a fix applied after `run start` has no effect on that process — check the scheduler's
  start time against the file mtime before telling anyone the run is fixed. Never hand over
  a `run start` line until the fix it depends on has been executed against the real binary
  in the real worktree.

## Historical — 2026-09-04 run a33d5e9b (a green suite is not a verdict about the code)

`lane-wp1-observations-build` recorded this gate row:

```
review[-1]  seq=3 executed=11 passed=11 failed=0 errored=0 reviewer=REVISE recorded=PASS agree=False
```

Eleven cases, eleven passes, and the final integration review said REVISE. The two
readers disagreed because they were asked different questions. The lane's code review
asks whether the sealed suite passes; it did. The final reviewer reads the code against
the plan's claims, and saw `ObservationStore.record(observation, *, raw_spl=None)` — a
signature under which an observation is appended with a null `spl_sha256`, and
`_normalise` lets `release_id`, `method_version` and `code_sha256` persist as null.

`claim-wp1-provenance` says every observation carries those three. Eleven cases asserted
that a **correctly called** store records them. Not one asserted that an incorrectly
called store refuses. A suite built only from the happy path cannot tell *required* from
*accepted when supplied*, so the builder took the weaker reading — legitimately, because
nothing in the contract or the cases forbade it. Three rounds of test review passed that
suite.

Two things follow, and neither is a new gate:

- **`agree=False` on a review row is the signal, not noise.** A PASS from the runner beside
  a REVISE from a reader means the suite does not encode what the claim says. Read the
  reader, not the count.
- **The fix belongs in the plan, not the builder.** Naming the obligation as *required*,
  and requiring refusal cases beside the positive ones, is what makes the next suite able
  to fail a permissive implementation. Patching the builder leaves the blind suite in
  place to mislead the next candidate.

Same run, one finding was false: the reviewer reported the build lane's gate exiting 4
because `services/label-batch/tests/observations` does not exist on the integration
surface. `_failed_run_gates` (`scheduler.py`) overlays the sealed bundle onto the
integration head before running it, and it had already passed — a gate failure there
produces exactly one fixed finding, `_INTEGRATION_GATE_REVISE`, which this verdict did
not carry. **A located finding from a reviewer is a claim, not a measurement.** Check it
against the factory's own row before acting on it.
