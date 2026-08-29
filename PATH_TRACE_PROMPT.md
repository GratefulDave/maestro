# Prompt: trace a maestro lane path end to end and find every gate that will refuse

Give this verbatim to another coder. Fill in the three bracketed values.

---

You are auditing one execution path in the maestro artifact-factory runtime. **Do not write a
fix. Do not run the workflow. Produce a table.**

- Repo (deployed instance): `[REPO PATH]`
- Ledger: `[<runtime_state_root>/lifecycle.sqlite3]` from the deployment's absolute `runtime_state_root` (not a template tree)
- Run and lane: `[run-… / lane-…]`

## What you are being asked for

Not "why did it fail". A path has more gates than it has bugs, and the first
sufficient explanation is not the explanation. Two independent causes routinely
produce a byte-identical refusal string. I want **every** gate between the current
durable state and the outcome the lane is trying to reach, each with the *actual
value* it will see, and a verdict of PASS or REFUSE for each.

## Method — in this order, no skipping

**1. Read the durable state. Never pane text, never an agent's claim, never process
liveness, never a dirty worktree.**

The persisted `lane_state.stage` field is the sole durable workflow authority.
Git commits and sealed artifact digests identify immutable inputs and outputs; they
do not independently encode stage. Operator verbs are only `run start`, `run resume`,
`run amend`, and `run status`.

```bash
sqlite3 -header <ledger> "select lane_id, stage from lane_state where run_id='<run>' order by lane_id;"
sqlite3 -header <ledger> "select sequence, lane_id, artifact_kind, input_digest, artifact_ref, payload_json
  from lane_artifacts where run_id='<run>' order by sequence desc limit 30;"
sqlite3 -header <ledger> "select sequence, artifact_kind, input_digest, artifact_ref
  from run_artifacts where run_id='<run>' order by sequence desc limit 30;"
```

Note timestamps — they are UTC. Convert to local and compare against the mtime of every runtime file
you are about to reason about. **Python binds modules at import: a fix written after
a scheduler started did not run in it.** A run whose artifacts predate the file's
mtime tells you nothing about the current code.

**2. Find the entry point in `adws/adw_modules/scheduler.py` and read forward, not
around.** Start at `FactoryScheduler.run` / `_advance` for the lane's current
`lane_state.stage` and follow the *first* branch whose predicate the durable state
satisfies. Write down each branch predicate and evaluate it against real values as
you go. Do not jump to the function whose name matches the error.

**3. Enumerate every gate.** A gate is anything that can raise, return `False`, or
refuse a frozen transition. For this runtime they cluster in five places, and you must
check all five even when the first one explains the symptom:

- **Lane stage** — `lane_state.stage` is exactly one of `PLANNED`, `WRITING_TESTS`,
  `REVIEWING_TESTS`, `TESTS_SEALED`, `BUILDING`, `REVIEWING_CODE`, `READY_TO_MERGE`,
  `MERGED`, `WAITING_FOR_USER`. Reviewer verdicts are artifact data (`PASS`/`REVISE`),
  not stages. Review roles are stages of the lane, never synthetic DAG nodes.
  `run resume` continues the next incomplete stage from the last accepted immutable
  artifact. After `PAUSE` it restores that stage/input. After `AMENDMENT_REQUIRED` it
  leaves the lane waiting. There is no retry/skip/abandon/cancel/bootstrap/plan
  subcommand, attempt identity, recovery marker, or actor generation to consult.
- **Immutable artifacts** — required input artifacts for the current stage
  (`LANE_PLAN`, `TEST_DRAFT`, `TEST_REVIEW`, `SEALED_TEST_BUNDLE`, `BUILDER_OUTPUT`,
  `CODE_REVIEW`, `INTEGRATION_MERGE`, run `FINAL_INTEGRATION_REVIEW` /
  `MAIN_PUBLICATION` / `PLAN_AMENDMENT` / `USER_WAIT` as applicable). Check input
  digest, artifact ref, and payload against the frozen transition table. Sealed
  private tests are vault digest/reference plus public contract; private bytes are
  absent from the run repo and builder input.
- **Git refs, not mutable branches** — immutable candidate ref/SHA on
  `BUILDER_OUTPUT`; run integration ref `refs/maestro/integration/<run-id>`;
  receipt-backed publication ref
  `refs/maestro/publications/<run-id>/<review-input-fingerprint>` plus
  `MAIN_PUBLICATION`. Never treat a mutable candidate branch, live pane, or dirty
  worktree as stage. `main` at the same SHA without Maestro's receipt is external
  activity and is refused.
- **Runtime binding** — `runtime_state_root` is the absolute external directory from
  the deployment config. Revalidate `runtime_state_fingerprint` before reading or
  mutating run state. Ledger, vault, locks, receipts, and ephemeral worktree roots
  live only there. Do not adopt an old worktree; resume recreates the stage from its
  last immutable input.
- **Readiness and merge serialization** — a lane is ready when its stage is not
  `MERGED` or `WAITING_FOR_USER` and every `needs` lane is `MERGED`. Independent ready
  lanes may author/review/build concurrently. Integration merge, final review, and
  publication are serialized. Dependent builder bases are the accepted integration
  artifact.

**4. Cross-check the invariants that hold *between* those rows.** This is where
the defects that survive a green suite live. At minimum:

- current stage matches the last committed stage-advancing artifact for that lane;
- required immutable inputs for that stage exist and their digests match the
  transition's input fingerprint;
- sealed-test digest on builder/code-review paths is the vault reference, not private
  source;
- candidate SHA named by `BUILDER_OUTPUT` equals the immutable candidate ref;
- integration `before_sha`/`after_sha` match the integration ref;
- publication SHA matches the receipt ref; same SHA on `main` without a receipt is
  not success;
- resume would start the current incomplete stage from those artifacts, not from a
  live agent or dirty tree.

State each invariant, then evaluate it. An invariant nothing checks is where the
next four hours go.

**5. Confirm coverage before trusting the suite.** For each function on the path:
`grep -l <function-or-class> tests/*.py`. A green suite proves nothing about a code
path no test executes, and "N passed" over fakes that bypass the real adapter proves
nothing at all.

## Output

One table, one row per gate, in execution order:

| # | file:line | gate / predicate | actual value here | PASS / REFUSE | refusal code and next stage if any |
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
