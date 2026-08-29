# Maestro Artifact Factory Contract

You implement and operate Maestro as a deliberately minimal, artifact-driven software factory. [`MAESTRO_architecture.md`](MAESTRO_architecture.md) is the architecture. This file is the agent-facing contract. Architectural deviation is failure, not initiative.

## Goal

Prove exactly two dependent lanes:

1. a private test-author / test-reviewer revision loop
2. accepted tests sealed and hidden from the implementation builder
3. a builder / code-reviewer revision loop with actionable feedback that does not leak private test source, fixtures, selectors, or expected literals
4. each accepted lane merged exactly once into the integration branch
5. dependent lane execution using the accepted integration artifact
6. final integration review of all completed lanes before publication to main
7. interrupted work restarting from its last immutable completed-stage artifact, not from a live agent or dirty worktree
8. a user amendment: a changed lane invalidates every former input and restarts its changed projection at `PLANNED`; policy-selected unchanged dependents revalidate

No broader framework, general-purpose recovery system, compatibility layer, or speculative extension belongs in this slice.

## Product flow

1. `deep-interview`, `arch-brownfield`, `planf3`, and `arch-review` produce one approved executable plan revision.
2. The plan compiler validates only objective properties: schema, DAG acyclicity, existing dependencies, declared outputs, non-conflicting file ownership, public acceptance criteria, and deterministic integration order.
3. Every ready lane executes private test author → test review → test sealing → builder → code review.
4. Reviewer `REVISE` returns to the author or builder with actionable, redacted feedback. `PASS` advances the lane.
5. Accepted lane commits merge exactly once into one run-specific integration branch.
6. A dependent lane starts from the integration commit containing every merged dependency.
7. When every lane is `MERGED`, a final reviewer evaluates the integration commit with all sealed tests. `PASS` permits exactly-once receipt-backed publication of that SHA to `main`. `REVISE` waits for a user amendment.
8. Process death restarts the current incomplete stage from its last immutable input.

## Authoritative lane stage enum

Persisted `lane_state.stage` is the sole durable workflow authority. Exactly these nine values:

```text
PLANNED
WRITING_TESTS
REVIEWING_TESTS
TESTS_SEALED
BUILDING
REVIEWING_CODE
READY_TO_MERGE
MERGED
WAITING_FOR_USER
```

The stage names the work that happens next. Completing a stage writes its immutable artifact and the next stage atomically.

`REVISE` is artifact data, not a stage. Git commits and sealed digests identify immutable bytes; they do not independently encode workflow stage.

Any new durable field, duplicated authority, speculative restart path, spend ceiling, actor generation, live-session adoption, dirty-worktree adoption, generic semantic gate, or second candidate identity requires both explicit user approval and a named acceptance scenario that cannot be satisfied without it. Absent both, reject or delete it.

Resume means starting the next incomplete stage from the last accepted immutable artifact. Never resurrect an agent process, reconstruct a consumed marker, or preserve uncommitted worktree state.

## Reviewer mandate

Reject:

- undeclared durable state
- duplicate representations of stage, attempt identity, candidate identity, review identity, or ownership
- adoption of agents, panes, sessions, or dirty worktrees
- abstractions not required by the two-lane slice
- speculative failure handling without a named acceptance scenario
- spend ceilings that prevent explicit user continuation
- generic reachability or semantic heuristics used as hard workflow authority
- private-test leakage to the builder
- reviewer feedback that is not actionable
- tests that validate implementation plumbing, source text, mocks, or incidental state instead of observable factory behavior
- more than one merge of an accepted lane artifact
- changes that expand scope while claiming to simplify it

`REVISE` findings must name violated requirement, observed behavior, required behavior, and implementation area. They must not include private test source, fixtures, selectors, expected literals, or vault paths.

## Pause and amendment

- Explicit pause appends `USER_WAIT` with `wait_reason=PAUSE`, `resume_stage`, and the complete immutable `resume_input_digest`, and moves the lane to `WAITING_FOR_USER`. `run resume <run-id>` restores that recorded stage/input.
- Final-review `REVISE` appends `USER_WAIT` with `wait_reason=AMENDMENT_REQUIRED`. Bare `run resume` leaves it waiting. Only `run amend` resolves it. Named lanes are already `MERGED`, so `needs`/output changes are refused. The amendment must change `spec_digest` of every named lane (which changes `lane_projection_digest`) or refuse `AMENDMENT_DOES_NOT_ADDRESS_REVIEW`. A valid amendment moves every named lane to `PLANNED`.
- Changed lanes restart at `PLANNED`. Unchanged dependents at `BUILDING`, `REVIEWING_CODE`, or `READY_TO_MERGE`, and unchanged already-`MERGED` dependents, revalidate from `BUILDING`. Unchanged unstarted dependents (`PLANNED`, `WRITING_TESTS`, `REVIEWING_TESTS`, `TESTS_SEALED`) keep their stage. Independently paused lanes stay `WAITING_FOR_USER` until explicit resume after dependencies re-merge.
- Published runs are immutable. Removing a lane from an existing run is refused. Adding a valid downstream lane is allowed.

## Operator commands

Execute only from a stamped deployment (`adws/maestro.py`) whose canonical Git common directory equals the `--repo` worktree's canonical Git common directory. `--repo` is the dedicated publication worktree, distinct from implementation-agent worktrees and from this Maestro checkout, the-library, or any other template tree. Never invoke `run start` from a template checkout:

```text
uv run adws/maestro.py run start <approved-plan> --repo <target-worktree-root> --main-ref <ref>
uv run adws/maestro.py run resume <run-id>
uv run adws/maestro.py run amend <approved-plan> --run <run-id>
uv run adws/maestro.py run status <run-id>
```

Template-source `run start` refuses `RUN_REPOSITORY_MISMATCH`. Opening a legacy ledger for execution refuses `LEDGER_SCHEMA_UNSUPPORTED`. Preserve it read-only. Start a new run/database. Do not guess a mapping from prior rows.

`run start` creates `refs/maestro/integration/<run-id>` from zero (`INTEGRATION_REF_COLLISION` if occupied at another SHA). Each `BUILDER_OUTPUT` pins `refs/maestro/candidates/<run-id>/<lane-id>/<input-digest>`. Publication is `refs/maestro/publications/<run-id>/<review-input-fingerprint>` plus `MAIN_PUBLICATION` (`PUBLICATION_EXTERNAL_MISMATCH` if `main` matches without that receipt; `PUBLICATION_WORKTREE_LOCK_REFUSED` if the target-worktree lock fails). Final-review fingerprint is SHA-256 of canonical JSON (`schema_version` 1) over integration SHA, plan revision/digest, and ordered `{lane_id, spec_digest, public_contract_artifact_id, sealed_test_bundle_artifact_id}`. Invalidated stage input refuses `STALE_STAGE_INPUT`.

## Plan compiler — objective checks only

- Schema and required fields.
- Existing dependency IDs and acyclic DAG.
- Exact normalized repository-relative POSIX declared output paths.
- No absolute, empty, `.`, or `..` components; no duplicate, equal, ancestor, or descendant ownership conflicts across lanes.
- Public acceptance criteria and deterministic integration order.

Do not add a generic semantic or produced-symbol reachability admission gate.

## Durable authority

Implement the ledger and transitions in `MAESTRO_architecture.md` §§8–12. One `complete_stage` CAS owns ordinary lane advances. `complete_final_review`, `apply_amendment`, and `complete_publication` are the only multi-row exceptions.

Determinism belongs at stage boundaries: immutable inputs, immutable outputs, one authoritative stage, `PASS` or `REVISE` review verdicts, explicit `USER_WAIT`/`USER_DECISION` control artifacts, idempotent transitions, exactly-once merges, real-runner behavioral evidence.

Do not attempt to make model reasoning, terminal sessions, agent processes, or dirty filesystems deterministic.

Prefer deletion over adaptation, direct code over abstraction, stage restart over live-state continuation, and observable end-to-end behavior over internal checks.

## Forbidden

Do not implement or promise:

- parallel state/phase enums
- attempts as workflow authority
- salvage, late-envelope continuation, marker consumption
- live actor/pane/session/dirty-worktree adoption
- candidate/review/handoff tables as a second identity
- actor generations
- spend ceilings, floors, grants
- `retry`, `skip`, `abandon`
- synthetic review DAG nodes
- compatibility writers or in-flight ledger migration
- builder access to private tests

Herdr and OMP are transport. Pane text is never stage authority.

## Workspace

- Implementation worktree: this Maestro feature worktree.
- Do not edit `/Users/davidandrews/PycharmProjects/maestro` or `/Users/davidandrews/PycharmProjects/the-library`.
- Do not commit unless you are the integration owner.
- Mirror ADWS template changes into the-library with `.claude/skills/sssf/templates/adws/tools/runtime_sync.py`, never `cp` / `rsync` / `git apply`.
