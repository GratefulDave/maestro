# Prompt: trace a maestro lane path end to end and find every gate that will refuse

Give this verbatim to another coder. Fill in the three bracketed values.

---

You are auditing one execution path in the maestro ADW runtime. **Do not write a
fix. Do not run the workflow. Produce a table.**

- Repo (deployed instance): `[REPO PATH]`
- Ledger: `[~/.maestro/<install>/lifecycle.sqlite3]`
- Run and node: `[run-… / lane-…]`

## What you are being asked for

Not "why did it fail". A path has more gates than it has bugs, and the first
sufficient explanation is not the explanation. Two independent causes routinely
produce a byte-identical refusal string. I want **every** gate between the current
durable state and the outcome the lane is trying to reach, each with the *actual
value* it will see, and a verdict of PASS or REFUSE for each.

## Method — in this order, no skipping

**1. Read the durable state. Never pane text, never an agent's claim, never the
`attempts` table's summary fields.**

```bash
sqlite3 -header <ledger> "select id,node_id,from_state,to_state,reason,created_at,detail_json
  from transitions where run_id='<run>' order by id desc limit 30;"
```

`transitions.detail_json` carries the real refusal. Note the `created_at` values —
they are UTC. Convert to local and compare against the mtime of every runtime file
you are about to reason about. **Python binds modules at import: a fix written after
a scheduler started did not run in it.** A run whose transitions predate the file's
mtime tells you nothing about the current code.

**2. Find the entry point in `adws/adw_modules/scheduler.py` and read forward, not
around.** Start at `_attempt_body` and follow the *first* branch whose predicate the
durable state satisfies. Write down each branch predicate and evaluate it against
real values as you go. Do not jump to the function whose name matches the error.

**3. Enumerate every gate.** A gate is anything that can raise, return `False`, or
write a `BlockReason`. For this runtime they cluster in five places, and you must
check all five even when the first one explains the symptom:

- **Lifecycle state** — node `state`, `block_reason`, `pending_cause`, `lane_phase`;
  and whether that block reason is in `NON_RETRYABLE` (`scheduler_types.py`). A
  reason in that tuple is *not* cleared by `run resume`; `_EXITS` names its escapes.
  Note that `LifecycleStore.retry` does **not** consult either table.
- **Attempt identity** — `attempts.extra_json` recovery keys
  (`late_envelope_recovery`, `repair_handoff_recovery`, `undispatched_resume`),
  `attempt_sealed_output`, `base_sha`. Each key routes to a *different* body.
- **Immutable publications** — `lane_candidates` (`candidate_sha`,
  `parent_candidate_sha`, `builder_generation`) and `candidate_reviews` (`state`,
  `verdict`, `reviewer_generation`). `publish_candidate` refuses any replay whose
  parent **or generation** disagrees with the stored row.
- **Actor generations** — `current_actor_session` and the full
  `actor_sessions(actor_role=…)` generation list, for `builder` *and* `reviewer`.
  Then check that every generation referenced by a durable row
  (`repair_handoffs.builder_generation`, `candidate_reviews.reviewer_generation`)
  **has a session in that list**. A row naming a generation with no session is
  permanently undeliverable: `continue_node` resolves it to a session and raises
  `AttemptOwnershipLost` forever, because no future attempt can conjure that session
  either. This is invisible to every test that stubs the launcher.
- **Git** — the attempt worktree `HEAD` and `refs/heads/maestro/{run}/{node}/a{n}`.
  `prepare_descendant_candidate` requires **both** to already equal the candidate.
  Worktrees survive at
  `~/.maestro/<install>/runs/<run>/worktrees/<run>-<node>-a<N>/`; run the harness's
  own measurement there, under the real binary, for **every runner the plan can
  name** — a stubbed `subprocess.run` replays the stdout you scripted and cannot
  observe how a real tool parses argv.

**4. Cross-check the invariants that hold *between* those tables.** This is where
the defects that survive a green suite live. At minimum:

- `repair_handoffs.builder_generation` == `lane_candidates.builder_generation` for
  the same candidate.
- every generation named by a durable row has a matching `actor_sessions` row.
- a candidate's `parent_candidate_sha` is an ancestor of it in git.
- the attempt a resume path will reopen actually produced what that path expects it
  to have produced.

State each invariant, then evaluate it. An invariant nothing checks is where the
next four hours go.

**5. Confirm coverage before trusting the suite.** For each function on the path:
`grep -l <function-or-class> tests/*.py`. A green suite proves nothing about a code
path no test executes, and "N passed" over fakes that bypass the real adapter proves
nothing at all.

## Output

One table, one row per gate, in execution order:

| # | file:line | gate / predicate | actual value here | PASS / REFUSE | refusal string and its retry class |
|---|-----------|------------------|-------------------|---------------|------------------------------------|

Then, below it:

- **Invariant violations** found in step 4, each with the two disagreeing values.
- **Uncovered code** from step 5 — functions on this path with no test.
- **The single idea**, if the REFUSEs share one. Several refusals on one path are
  usually one wrong idea wearing different faces; say what the idea is in one
  sentence. If you cannot, say so rather than inventing one.

## Rules

- Report every REFUSE, not the first. Stopping at the first is the failure this
  audit exists to prevent.
- Every value in the table comes from a command you ran, not from reading code and
  predicting. Quote the command.
- Do not propose fixes and do not edit anything.
- Do not print a `run start` or `run resume` line.
