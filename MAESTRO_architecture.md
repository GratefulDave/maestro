# MAESTRO — Artifact Factory Contract

This document is the executable architecture. Runtime source must implement it directly. Historical SSSF sequential-phase prose, Strav archaeology, and any second lifecycle projection are not authority.

**Packaging:** the factory is stamped from `.claude/skills/sssf/templates/adws/` into a product repository as `adws/`. Operator execution is only from that deployment copy (`adws/maestro.py`). Invoking run creation from Maestro, the-library, or any other template source refuses `RUN_REPOSITORY_MISMATCH`.

**Proven slice:** exactly two dependent lanes. No broader framework.

**Diagrams:** the whole mechanism on one page — every stage, the artifact or verdict that causes every transition, and the private-test boundary — is Figure 1 of [`docs/architecture/00-artifact-factory.html`](docs/architecture/00-artifact-factory.html), which also carries the durable-state topology; nine-stage machine — [`docs/architecture/00-lane-stage.html`](docs/architecture/00-lane-stage.html); persistent role topology — [`docs/architecture/00-role-topology.html`](docs/architecture/00-role-topology.html). The rest of [`docs/architecture/`](docs/architecture/index.html) is historical / superseded.

---

## 1. Product flow

1. `deep-interview`, `arch-brownfield`, `planf3`, and `arch-review` produce one approved executable plan revision.
2. The plan compiler validates only objective properties (§7).
3. Untyped lanes execute the universal private test author → test review → test sealing → builder → code review lifecycle. An authored `lane_kind=tests` lane routes `PLANNED → WRITING_TESTS → REVIEWING_TESTS → TESTS_SEALED → MERGED` and emits `SEALED_TEST_BUNDLE` without builder, code review, or integration merge. An authored `lane_kind=build` lane routes `PLANNED → BUILDING → REVIEWING_CODE → READY_TO_MERGE → MERGED`, skipping test author and test reviewer. Absent `lane_kind` keeps the universal lifecycle.
4. A reviewer `REVISE` verdict returns to the author or builder with actionable, redacted feedback. A `PASS` verdict advances the lane.
5. Accepted **build** and untyped lane commits merge exactly once into one run-specific integration branch. A typed tests lane is `MERGED` after `SEALED_TEST_BUNDLE`; dependents treat that current-revision sealed bundle as readiness instead of an integration merge receipt.
6. A dependent lane starts when every predecessor is `MERGED`. Tests-kind dependencies supply the current-revision `SEALED_TEST_BUNDLE`; build-kind and untyped dependencies supply the current-revision `INTEGRATION_MERGE` receipt.
7. When every lane is `MERGED`, a final `integration-reviewer` launches lazily in the last topological integration-order lane's child workspace (not `lanes[0]`, not a separate integration workspace) and evaluates the integration commit with all sealed tests. `PASS` permits exactly-once publication of that reviewed SHA to `main`; `REVISE` retains every affected lane's role sessions and waits for a user amendment. Cleanup does not run on `MERGED`.
8. Process death restarts the current incomplete stage from its last immutable input. A still-running role pane may reconnect as transport by proved identity. Unknown, mismatched, or dirty worktrees and unproved agents are refused.
9. Every authenticated OMP or Claude role process remains visible in its host Herdr pane and receives only Bash. A mandatory hook runs each model-issued shell command in a disposable OrbStack/Docker container with no network, a read-only container root, scrubbed credentials, checkout-local scratch directories, hidden Git metadata, and only that role's stable worktree or private tree mounted writable. Sibling role trees, the target repository, runtime state, vaults, host credentials, coder installations, and publication state are absent. Missing Docker, image, hook, or confinement refuses launch.

This is a clean replacement, not a compatibility layer.

---

## 2. One authoritative lane stage enum

The persisted `lane_state.stage` field is the sole durable workflow authority. Git commits and sealed artifact digests identify immutable inputs and outputs; they do not independently encode workflow stage.

Exactly these nine values exist:

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

The stage names the work that must happen next. A stage is complete only when its immutable output artifact and the next stage are committed atomically.

`REVISE` is artifact data, not a stage. Review roles are stages of the lane, never synthetic DAG nodes.

No second field may independently describe lane position: not a parallel state enum, phase enum, pending cause, block reason, retry class, review-dispatch state, repair-handoff state, actor-session state, run-outcome field, attempt number, candidate sequence, generation, floor, or grant.

---

## 3. Frozen state-transition table

Every legal lane advance is one of these edges. Trigger, required immutable input, emitted artifact, reviewer verdict, and next stage are named. Missing cells are not implicit second authorities.

This table is drawn as Figure 1 of [`docs/architecture/00-artifact-factory.html`](docs/architecture/00-artifact-factory.html), with the private-test boundary of §11 and the sealing gate that is its only exit. The table is authority; the figure is a reading of it.

| Current stage | Trigger | Required immutable input | Emitted artifact | Reviewer verdict | Next stage |
|---|---|---|---|---|---|
| `PLANNED` | materialize lane plan | approved plan artifact, active plan digest/revision, lane spec digest, ordered `needs`, ordered declared outputs | `LANE_PLAN` | none | `WRITING_TESTS` (untyped or `lane_kind=tests`); `BUILDING` when authored `lane_kind=build` |
| `PLANNED` | explicit pause | complete `PLANNED` input | `USER_WAIT` (`wait_reason=PAUSE`) | none | `WAITING_FOR_USER` |
| `WRITING_TESTS` | test author completes | current `LANE_PLAN`; latest actionable `TEST_REVIEW(REVISE)` or `NO_TEST_REVIEW`; latest `TEST_INVALIDATION` newer than the latest `TEST_DRAFT`, or `NO_TEST_INVALIDATION` | `TEST_DRAFT` (private draft digest/reference plus public behavioral contract) | none | `REVIEWING_TESTS` |
| `WRITING_TESTS` | explicit pause | complete `WRITING_TESTS` input | `USER_WAIT` (`PAUSE`) | none | `WAITING_FOR_USER` |
| `REVIEWING_TESTS` | test reviewer accepts | current `LANE_PLAN` and `TEST_DRAFT` | `TEST_REVIEW` | `PASS` | `TESTS_SEALED` |
| `REVIEWING_TESTS` | test reviewer rejects | current `LANE_PLAN` and `TEST_DRAFT` | `TEST_REVIEW` (actionable findings) | `REVISE` | `WRITING_TESTS` |
| `REVIEWING_TESTS` | explicit pause | complete `REVIEWING_TESTS` input | `USER_WAIT` (`PAUSE`) | none | `WAITING_FOR_USER` |
| `TESTS_SEALED` | seal accepted tests | current `LANE_PLAN`, `TEST_DRAFT`, passing `TEST_REVIEW` | `SEALED_TEST_BUNDLE` (vault digest/reference; private bytes absent from run repo and builder input) | none | `BUILDING` (untyped); `MERGED` when authored `lane_kind=tests` |
| `TESTS_SEALED` | explicit pause | complete `TESTS_SEALED` input | `USER_WAIT` (`PAUSE`) | none | `WAITING_FOR_USER` |
| `BUILDING` | builder completes | exact `BUILDING` variant fingerprint (§8.6) | `BUILDER_OUTPUT` bound to plan revision, base SHA, sealed-test digest, immutable candidate ref/SHA | none | `REVIEWING_CODE` |
| `BUILDING` | `CODE_REVIEW(REVISE)` and next declared-output tree byte-identical to the prior candidate | current `CODE_REVIEW(REVISE)` plus prior `BUILDER_OUTPUT` | none; refuses `NOOP_BUILDER_REVISION` before a new `BUILDER_OUTPUT` | none | remains `BUILDING` (review retained, reviewer not relaunched) |
| `BUILDING` | explicit pause | complete `BUILDING` input | `USER_WAIT` (`PAUSE`) | none | `WAITING_FOR_USER` |
| `REVIEWING_CODE` | code reviewer accepts | current `LANE_PLAN`, `SEALED_TEST_BUNDLE`, `BUILDER_OUTPUT`; builder base SHA; candidate ref/SHA | `CODE_REVIEW` (private test results retained for the reviewer; public payload redacted) | `PASS` | `READY_TO_MERGE` |
| `REVIEWING_CODE` | code reviewer rejects | same as accept | `CODE_REVIEW` (redacted actionable findings) | `REVISE` | `BUILDING` |
| `REVIEWING_CODE` | private-path collision | current `LANE_PLAN`, `SEALED_TEST_BUNDLE`, `BUILDER_OUTPUT`; builder base SHA; candidate ref/SHA | `TEST_INVALIDATION` (redacted actionable reason; prior artifacts remain immutable history) | none | `WRITING_TESTS` |
| `REVIEWING_CODE` | explicit pause | complete `REVIEWING_CODE` input | `USER_WAIT` (`PAUSE`) | none | `WAITING_FOR_USER` |
| `READY_TO_MERGE` | integration merge | current `BUILDER_OUTPUT` and passing `CODE_REVIEW`; builder base SHA; candidate ref/SHA; integration HEAD observed for this decision | `INTEGRATION_MERGE` (`before_sha`, accepted candidate SHA, `after_sha`) | none | `MERGED` |
| `READY_TO_MERGE` | stale zero-delta base | stale `BUILDER_OUTPUT` and passing `CODE_REVIEW`; stale base/candidate SHA; newly observed integration HEAD | `BASE_INVALIDATION` | none | `BUILDING` |
| `READY_TO_MERGE` | explicit pause | complete `READY_TO_MERGE` input | `USER_WAIT` (`PAUSE`) | none | `WAITING_FOR_USER` |
| `MERGED` | lane complete | none at lane level | none | none | remains `MERGED` |
| `MERGED` | final-review `REVISE` names this lane | active final-review fingerprint; passing-all-merged predicate | run `FINAL_INTEGRATION_REVIEW` plus per-named-lane `USER_WAIT` (`wait_reason=AMENDMENT_REQUIRED`) | `REVISE` | `WAITING_FOR_USER` |
| `PLANNED` / `WRITING_TESTS` / `REVIEWING_TESTS` / `TESTS_SEALED` / `BUILDING` / `REVIEWING_CODE` / `READY_TO_MERGE` | `apply_amendment` changed unmerged projection | canonical `PLAN_AMENDMENT`; every former input invalidated | `PLAN_AMENDMENT`; new `LANE_PLAN` required before authoring | none | `PLANNED` |
| `PLANNED` | `apply_amendment` unchanged unstarted dependent | canonical `PLAN_AMENDMENT` | `PLAN_AMENDMENT` | none | remains `PLANNED` |
| `WRITING_TESTS` | `apply_amendment` unchanged unstarted dependent | canonical `PLAN_AMENDMENT` | `PLAN_AMENDMENT` | none | remains `WRITING_TESTS` |
| `REVIEWING_TESTS` | `apply_amendment` unchanged unstarted dependent | canonical `PLAN_AMENDMENT` | `PLAN_AMENDMENT` | none | remains `REVIEWING_TESTS` |
| `TESTS_SEALED` | `apply_amendment` unchanged unstarted dependent | canonical `PLAN_AMENDMENT` | `PLAN_AMENDMENT` | none | remains `TESTS_SEALED` |
| `BUILDING` | `apply_amendment` unchanged started authored `build` dependent | canonical `PLAN_AMENDMENT` | `PLAN_AMENDMENT` | none | `BUILDING` |
| `BUILDING` | `apply_amendment` unchanged started untyped dependent | canonical `PLAN_AMENDMENT` | `PLAN_AMENDMENT` | none | `TESTS_SEALED` |
| `REVIEWING_CODE` | `apply_amendment` unchanged started authored `build` dependent | canonical `PLAN_AMENDMENT` | `PLAN_AMENDMENT` | none | `BUILDING` |
| `REVIEWING_CODE` | `apply_amendment` unchanged started untyped dependent | canonical `PLAN_AMENDMENT` | `PLAN_AMENDMENT` | none | `TESTS_SEALED` |
| `READY_TO_MERGE` | `apply_amendment` unchanged started authored `build` dependent | canonical `PLAN_AMENDMENT` | `PLAN_AMENDMENT` | none | `BUILDING` |
| `READY_TO_MERGE` | `apply_amendment` unchanged started untyped dependent | canonical `PLAN_AMENDMENT` | `PLAN_AMENDMENT` | none | `TESTS_SEALED` |
| `MERGED` | `apply_amendment` changed projection | canonical `PLAN_AMENDMENT` with changed `spec_digest` (hence `lane_projection_digest`); `needs`/output changes refused because the lane is already merged | `PLAN_AMENDMENT`; new `LANE_PLAN` required before authoring | none | `PLANNED` |
| `MERGED` | `apply_amendment` unchanged already-merged authored `build` dependent | canonical `PLAN_AMENDMENT` | `PLAN_AMENDMENT` | none | `BUILDING` |
| `MERGED` | `apply_amendment` unchanged already-merged untyped or authored `tests` lane | canonical `PLAN_AMENDMENT` | `PLAN_AMENDMENT` | none | `TESTS_SEALED` |
| `WAITING_FOR_USER` | `run resume` after `PAUSE` | matching `USER_DECISION` over the recorded `USER_WAIT` | `USER_DECISION` | none | recorded `resume_stage` with recorded complete input |
| `WAITING_FOR_USER` | `run amend` after `AMENDMENT_REQUIRED` | `PLAN_AMENDMENT` that changes `spec_digest` of every named lane (hence `lane_projection_digest`); named lanes are already `MERGED` so `needs`/output changes are refused | `PLAN_AMENDMENT` | none | `PLANNED` |
| `WAITING_FOR_USER` | `apply_amendment` changed projection while independently `PAUSE`-waiting | canonical `PLAN_AMENDMENT` | `PLAN_AMENDMENT`; new `LANE_PLAN` required before authoring | none | `PLANNED` |
| `WAITING_FOR_USER` | `apply_amendment` unchanged projection while independently `PAUSE`-waiting | canonical `PLAN_AMENDMENT` | replacement `USER_WAIT` plus amendment decision naming the invalidated input digest and policy-selected restart stage | none | remains `WAITING_FOR_USER` |
| `WAITING_FOR_USER` | bare `run resume` after `AMENDMENT_REQUIRED` | none accepted | none | none | remains `WAITING_FOR_USER` |

Normal test-review and code-review repairs do not require plan amendments. They loop directly to `WRITING_TESTS` or `BUILDING`. Private-path collision invalidation also loops to `WRITING_TESTS` without a plan amendment.

`BASE_INVALIDATION` and `TEST_INVALIDATION` never pause the lane.

---

## 4. Reviewer mandate

Review data is only `PASS` or actionable `REVISE`.

A `REVISE` finding must include:

- violated requirement
- observed behavior
- required behavior
- implementation area

A `REVISE` finding must not include:

- private test source
- fixtures
- selectors
- expected literals
- vault paths

The builder receives only its own product lane spec, public criteria, declared outputs, predecessor sealed-bundle ID and digest, architecture constraints, allowed paths, and prior redacted review feedback. It does not receive private source, vault paths, private manifests, or tests-lane declared paths. `REVIEWING_CODE` runs the predecessor sealed suite in scratch; that broker result is authoritative for the candidate.

The adversarial reviewer of this factory itself must reject undeclared durable state, duplicate stage/identity representations, unproved live-process or dirty-worktree adoption, durable `actor_sessions` or actor generations, pane occupancy as acceptance, abstractions not required by the two-lane slice, speculative failure handling without a named acceptance scenario, budgets that prevent explicit user continuation, generic reachability or semantic heuristics used as hard workflow authority, private-test leakage, non-actionable feedback, plumbing-only tests, more than one merge of an accepted lane artifact, and scope expansion claimed as simplification.

---

## 5. Pause and amendment policy

### Explicit pause

An explicit pause appends `USER_WAIT` containing `wait_reason=PAUSE`, `resume_stage`, and the complete immutable `resume_input_digest`, and moves the lane to `WAITING_FOR_USER`. A subsequent explicit `run resume <run-id>` appends or exact-replays a `USER_DECISION` and atomically restores that recorded stage and input.

### Final-review amendment wait

A final-review `REVISE` appends `USER_WAIT` with `wait_reason=AMENDMENT_REQUIRED`. Bare `run resume` leaves the lane waiting. Only `run amend` can resolve this wait. Named lanes are already `MERGED`, so dependency or declared-output changes are refused. The amendment must change the `spec_digest` of every named lane (which changes `lane_projection_digest`) or refuse `AMENDMENT_DOES_NOT_ADDRESS_REVIEW`. A valid amendment moves every named lane to `PLANNED`.

`run amend` is a separate explicit verb. Ordinary stage-boundary continuation is `run resume <run-id>`.

### Changed versus unchanged lanes

A changed lane is one whose canonical spec, ordered `needs`, ordered declared outputs, or authored `lane_kind` differ, and therefore whose `lane_projection_digest` changes. Absent `lane_kind` keeps the legacy digest identity. It restarts at `PLANNED`, invalidates every former input, and creates a new `LANE_PLAN` before the typed or untyped next stage. No input from a changed projection may be retained. Changed-projection reset to `PLANNED` takes precedence over pause preservation.

An unchanged transitive dependent whose implementation has not started (`PLANNED`, `WRITING_TESTS`, `REVIEWING_TESTS`, or `TESTS_SEALED`) keeps its current stage. Its spec/test artifacts remain valid. Its eventual builder reads the new integration HEAD.

An unchanged authored `build` dependent at `BUILDING`, `REVIEWING_CODE`, `READY_TO_MERGE`, or `MERGED` restarts at `BUILDING` because its dependency base changed. An unchanged untyped dependent at those stages resets to `TESTS_SEALED` so the scheduler re-emits a current-revision `SEALED_TEST_BUNDLE` and then continues `BUILDING`.

An unchanged lane that was independently paused with `wait_reason=PAUSE` stays `WAITING_FOR_USER`. Amendment atomically appends a replacement `USER_WAIT` plus an amendment decision entry naming the invalidated input digest and policy-selected restart stage. Resume remains blocked until dependencies have re-merged, then derives the new complete immutable input fingerprint, appends `USER_DECISION`, and restores that restart stage. It never exact-replays the stale pre-amendment input or silently discards the pause. A changed paused lane resets to `PLANNED` instead. This rule does not apply to a final-review `AMENDMENT_REQUIRED` wait, which resolves only through the changed-projection `PLANNED` reset.

An unchanged authored `build` dependent already at `MERGED` is atomically reset to `BUILDING` by `apply_amendment`, in the same transaction that selects the new plan revision. An unchanged authored `lane_kind=tests` lane, and an unchanged untyped lane already at `MERGED`, reset to `TESTS_SEALED` so the scheduler re-emits a current-revision `SEALED_TEST_BUNDLE`; typed tests then return `MERGED` before dependent builds start, untyped then continue `BUILDING`. A build lane is durably nonterminal immediately, but the normal `needs` predicate prevents builder dispatch until every amended upstream dependency has re-merged. Its builder may then emit a measured-zero-delta `BUILDER_OUTPUT` whose candidate SHA equals the new integration base. Code review still runs. A passing no-change result advances through `READY_TO_MERGE` using an `INTEGRATION_MERGE` artifact with `before_sha == candidate_sha == after_sha` and `revalidated=true`; no dummy commit is created.

Every lane named by a final-review `REVISE` is a changed lane: after a valid pre-publication amendment it restarts at `PLANNED`, creates a revised `LANE_PLAN`, and later supersedes its prior integration content without rewriting history. There is no discretionary unchanged-projection reset for a final-review finding.


### Topology and publication immutability

Already published runs are immutable. Amendments after `MAIN_PUBLICATION` or a reconciled publication receipt are refused and require a new run.

Dependency or declared-output changes to an already merged lane are refused. The same changes to an unmerged lane are allowed only through the changed-projection `PLANNED` reset. Content amendments before final publication may reopen lanes as above. Adding a new downstream lane is allowed if the DAG remains valid. Removing any lane from an existing run is refused, regardless of its current stage or artifact history.

---

## 6. Run status (derived, not stored)

Run status is derived in this precedence:

1. complete if a `MAIN_PUBLICATION` artifact exists for the active final-review input fingerprint and its immutable Git receipt
2. waiting if any lane is `WAITING_FOR_USER`
3. executing if any lane is not `MERGED`
4. integration review pending if all lanes are `MERGED` and no passing final-review artifact exists for the active fingerprint
5. publishable if a passing final-review artifact exists for the active fingerprint

The final-review input fingerprint is SHA-256 of canonical JSON (`schema_version` 1) containing `integration_sha`, `plan_revision`, `plan_digest`, and ordered `lanes` of `{lane_id, spec_digest, public_contract_artifact_id, sealed_test_bundle_artifact_id}`. The observed target-main SHA is publication's expected-before value; it is not review identity.

No mutable `latest_outcome`, scheduler PID/host/claim, cancellation cause, review refresh count, or spend ceiling is stored as workflow authority.

---

## 7. Objective plan compiler checks

The compiler admits a plan if and only if all of the following hold. It does not judge produced-symbol reachability, narrative quality, or other generic semantics.

- Schema and required fields are present and well-typed.
- Optional authored `lane_kind` is only `tests` or `build`. Absent `lane_kind` is the legacy universal lifecycle. `lane_projection_digest` includes `lane_kind` only when authored.
- An authored `build` lane has exactly one direct `tests` dependency and may additionally depend on `build` lanes.
- Every `needs` ID exists in the same plan.
- The dependency graph is acyclic.
- Declared outputs are exact normalized repository-relative POSIX file paths, never directories or globs.
- Paths have no absolute, empty, `.`, or `..` components.
- No duplicate, equal, ancestor, or descendant ownership conflicts exist across lanes.
- Each lane declares public acceptance criteria.
- Integration order is deterministic from the DAG.

Runtime path comparison is byte-exact after that normalization. It never follows a candidate symlink.

---

## 8. Durable data model

New ledger schema version. Do not migrate in-flight legacy runs into guessed stages.

### 8.1 `runs`

Retain only run identity and immutable/current plan/integration/repository facts:

- `run_id` primary key
- `runtime_state_root`
- `runtime_state_fingerprint`
- `plan_digest`
- `plan_revision`
- `integration_ref`
- `integration_initial_sha`
- `target_repository_root`
- `target_git_common_dir`
- `target_worktree_git_dir`
- `target_object_format`
- `target_repository_fingerprint`
- `target_sync_journal_fingerprint`
- `target_initial_main_sha`
- `target_main_ref`
- `created_at`, `updated_at`

Deferred composite foreign key `(run_id, plan_revision) -> plan_revisions(run_id, plan_revision)`.

### 8.2 `plan_revisions`

Append-only approved plan lineage: `(run_id, plan_revision)` primary key; `(run_id, plan_digest)` unique; `parent_revision` nullable for the initial plan; `plan_artifact_ref`; `amendment_artifact_id` nullable for the initial plan; `created_at`.

Initial revision has both parent and amendment NULL. Every later revision has both non-NULL. `apply_amendment` verifies same-run kind `PLAN_AMENDMENT`; a foreign key is not kind authority.

### 8.3 `dag_lanes`

Immutable projection of each approved plan revision: `(run_id, plan_revision, lane_id)` primary key; `needs_json`; `spec_digest`; `declared_outputs_json`; `lane_projection_digest` (SHA-256 of canonical JSON containing the normalized lane spec, ordered `needs_json`, and ordered `declared_outputs_json`).

`runs.plan_revision` selects the active projection. Rows are never overwritten.

### 8.4 `lane_state`

The only mutable lane authority: `(run_id, lane_id)` primary key; `stage` constrained to the nine values in §2; `updated_at`.

### 8.5 `lane_artifacts`

Append-only. `artifact_id` is SHA-256 of the canonical immutable artifact envelope. Unique `(run_id, lane_id, sequence)`. Completion key `(run_id, lane_id, plan_revision, completed_stage, input_digest)` unique.

Required kinds: `LANE_PLAN`, `TEST_DRAFT`, `TEST_REVIEW`, `SEALED_TEST_BUNDLE`, `BUILDER_OUTPUT`, `CODE_REVIEW`, `INTEGRATION_MERGE`, `BASE_INVALIDATION`, `TEST_INVALIDATION`, `USER_WAIT`, `USER_DECISION`.

Private bytes are never stored in SQLite or the run repository. Byte-identical replay returns the existing row. Different content for the same completion key is a hard refusal.

Every `BUILDER_OUTPUT` records immutable product-repository ref `refs/maestro/candidates/<run-id>/<lane-id>/<input-digest>` pinning its candidate commit.

### 8.6 Canonical immutable input fingerprints

Every fingerprint is SHA-256 over UTF-8 canonical JSON with schema version `1`, lexicographically sorted object keys, no insignificant whitespace, explicit sentinel strings instead of omitted optionals, and arrays ordered as stated. Every lane-stage envelope starts with `run_id`, `lane_id`, `stage`, source `plan_revision`, `plan_digest`, `spec_digest`, `lane_projection_digest`, and `input_artifact_ids`.

| Stage/event | Additional canonical input members |
|---|---|
| `PLANNED` | approved `plan_artifact_ref`, canonical `needs` ordered by lane ID, canonical declared outputs ordered by path |
| `WRITING_TESTS` | current `LANE_PLAN` artifact ID and latest actionable `TEST_REVIEW(REVISE)` artifact ID, or `NO_TEST_REVIEW` on first entry; latest `TEST_INVALIDATION` artifact ID newer than the latest `TEST_DRAFT`, or `NO_TEST_INVALIDATION`; durable run integration tip (`integration_head`) |
| `REVIEWING_TESTS` | current `LANE_PLAN` and `TEST_DRAFT` artifact IDs |
| `TESTS_SEALED` | current `LANE_PLAN`, `TEST_DRAFT`, and passing `TEST_REVIEW` artifact IDs |
| `BUILDING` | exact `entry_kind` plus the members below |
| `REVIEWING_CODE` | current `LANE_PLAN`, `SEALED_TEST_BUNDLE`, and `BUILDER_OUTPUT` artifact IDs; builder base SHA; immutable candidate ref and SHA |
| `READY_TO_MERGE` | current `BUILDER_OUTPUT` and passing `CODE_REVIEW` artifact IDs; builder base SHA; immutable candidate ref and SHA; integration HEAD observed for this merge decision |
| `BASE_INVALIDATION` | stale `BUILDER_OUTPUT` and passing `CODE_REVIEW` artifact IDs; stale builder base/candidate SHA; newly observed integration HEAD |
| `PLAN_AMENDMENT` | prior active plan revision/digest, current integration HEAD, active final-review `REVISE` artifact ID or `NO_FINAL_REVIEW`, new approved plan digest/ref, complete canonical amended projection, ordered reset set, and complete ordered `retained_inputs`/`invalidated_inputs` sets |

`BUILDING` is constructed while holding the run-mutation and integration-ref locks. Common members: current `LANE_PLAN`, current `SEALED_TEST_BUNDLE`, captured `builder_base_sha`, and exactly one dependency receipt for each direct `needs` lane, ordered by dependency lane ID: that lane's highest-sequence `INTEGRATION_MERGE`, which must be the artifact that most recently completed its current `MERGED` state and whose `after_sha` is an ancestor of `builder_base_sha`. Then exactly one disjoint variant:

- `INITIAL`: `NO_PRIOR_BUILDER`, `NO_CODE_REVIEW`, `NO_BASE_INVALIDATION`. This variant consumes no historical builder or review, including after an amendment reset of a previously building or merged unchanged dependent. Older `BUILDER_OUTPUT` / `CODE_REVIEW` artifacts are forbidden in this input. The new plan revision, captured base, and selected dependency receipts make the input distinct.
- `CODE_REVISE`: prior `BUILDER_OUTPUT` and actionable `CODE_REVIEW(REVISE)` plus `NO_BASE_INVALIDATION`
- `BASE_INVALIDATION`: stale prior `BUILDER_OUTPUT`, its passing `CODE_REVIEW`, and the `BASE_INVALIDATION` artifact ID; `NO_CODE_REVIEW_REVISE`

### 8.7 `run_artifacts`

Append-only run-level records. Kinds: `FINAL_INTEGRATION_REVIEW`, `MAIN_PUBLICATION`, `PLAN_AMENDMENT`. Completion key `(run_id, artifact_kind, input_digest)` unique.

### 8.8 `transitions`

Append-only audit only. Never read to decide current state.

---

## 9. Atomic store operations

One method owns every ordinary lane advance:

```text
complete_stage(
  run_id,
  lane_id,
  expected_stage,
  expected_input_digest,
  artifact,
  next_stage,
)
```

Inside one `BEGIN IMMEDIATE` transaction: canonicalize and derive `artifact_id`; read `lane_state.stage`, active projection, and active `PLAN_AMENDMENT`; recompute the admissible complete immutable input fingerprint; require it to equal `expected_input_digest`; require matching active `spec_digest` and `lane_projection_digest`; require older-revision inputs to match a `retained_inputs` entry exactly or refuse `STALE_STAGE_INPUT`; exact-replay if already at `next_stage` with the identical artifact; otherwise validate the frozen edge, insert the artifact, CAS the one stage field, append audit, commit.

Three additional operations handle irreducibly multi-row decisions without adding another authority:

- `complete_final_review(...)` — require the active fingerprint; exact-replay the run artifact; `PASS` names no affected lanes; `REVISE` names a nonempty unique subset of active `MERGED` lanes and CAS each to `WAITING_FOR_USER` with `USER_WAIT`.
- `apply_amendment(...)` — acquire locks 1–3, reconcile any existing publication receipt, and refuse if publication has occurred; then under one `BEGIN IMMEDIATE` validate publication/topology and the canonical `PLAN_AMENDMENT`; require every final-review-named lane to have a changed `spec_digest` (which changes `lane_projection_digest`) and a `PLANNED` reset — named lanes are already `MERGED`, so `needs`/output changes are refused; insert the complete new projection; CAS affected lanes to policy-selected stages; release the locks only after that atomic reset.
- `complete_publication(...)` — require the still-active review fingerprint and exact immutable Git receipt; exact-replay `MAIN_PUBLICATION`.

No alias or compatibility writer remains.

---

## 10. Runtime binding

Run creation requires:

```text
uv run adws/maestro.py run start <approved-plan> --repo <target-worktree-root> --main-ref <ref>
```

The executing runtime must be a deployment whose canonical Git common directory equals the `--repo` worktree's canonical Git common directory. `--repo` is the dedicated publication worktree, distinct from implementation-agent worktrees and from Maestro/the-library template trees. Invoking run creation from a template source refuses `RUN_REPOSITORY_MISMATCH`.

`target_repository_root` is the canonical realpath of the exact non-bare publication worktree passed by `--repo`. Implementation agents never edit it. Run creation refuses unless its symbolic `HEAD` is the configured main ref. Every lane spec must declare one consistent safe `spec.integration.integration_branch`. Run creation resolves that branch once and pins its SHA as `integration_initial_sha`. `target_initial_main_sha` remains the separately recorded publication-main SHA and may differ.

The committed row owns integration ref `refs/maestro/integration/<run-id>`, created with `git update-ref` from zero to `integration_initial_sha`. An existing ref at any other SHA refuses `INTEGRATION_REF_COLLISION`. Tester worktrees and writing-test input identity use the durable run integration tip. Builders never receive private tests.

Scheduler start recovers a present `locks/legacy_integration_retarget.<run-id>.json` journal first: finish any Git/SQLite split of that retarget, restore the in-memory binding, hold lock 2, then unlink. Only then reconcile an orphaned integration merge, apply an ordinary plan-branch legacy-base correction, and ensure the run integration ref. The correction writes the journal, CAS-updates the integration ref, CAS-updates `runs.integration_initial_sha`, then unlinks. Unsafe rebase when any of `BUILDER_OUTPUT`, `CODE_REVIEW`, `INTEGRATION_MERGE`, `BASE_INVALIDATION`, `FINAL_INTEGRATION_REVIEW`, or `MAIN_PUBLICATION` exists refuses `LEGACY_INTEGRATION_REBASE_UNSAFE`. Correction while any lane is `REVIEWING_TESTS` is deferred. `TEST_REVIEW` including `REVISE` is not an unsafe kind; an active run may still migrate after that artifact.

The deployment-owned `adws/maestro.config.yaml` requires one absolute `runtime_state_root`. The deployed CLI canonicalizes and opens that existing directory without following symlinks, requires mode `0700`, and refuses if it is inside or overlaps the target repository root, any target Git/worktree directory, or either template-source checkout. `runtime_state_fingerprint` is SHA-256 over canonical realpath, device, and inode and is bound into every run. Every start, resume, amend, status, and publication operation revalidates it before reading or mutating run state.

New installations stamp `dashboard: {enabled, launcher absolute from the installed skill, api_port 4600, ui_port 4317, open}`. Old, missing, or invalid dashboard config warns and fails open. The launcher reuses only owned processes, never kills unknown port owners, ensures the Bun API `/api/sources` list contains the current canonical ledger plus Next `/runs` readiness, then opens `/runs`. API/UI logs and pid ownership live under user cache.

The ledger (`lifecycle.sqlite3` plus WAL/SHM), immutable file artifacts, private vault, locks, receipts, plans copied for execution, and stable role-scoped working-tree roots live only under that runtime-state root.

A lane is ready when its stage is not `MERGED` or `WAITING_FOR_USER` and every `needs` lane is `MERGED`. Independent ready lanes may execute author/review/build stages concurrently. Integration merges are serialized.

Every runner the plan names is proven usable before any agent is dispatched: one tree per distinct `(runner, gate cwd)`, provisioned and resolved at the scheduler entry both run verbs cross, refusing `RUNNER_PREFLIGHT_REFUSED` and naming what failed. This is a harness fault and is never delivered to an actor as a finding. A runner first exercised at draft collection charges an agent's whole turn for an environment it does not own and cannot repair: measured 2026-09-03, `lane-wp7-gw-dpa-tests` spent nine minutes writing a valid suite, was refused seven seconds later with `no usable pytest was found for .`, and was then handed a correction turn asking it to fix that.

A runner resolves against the tree it will run in when nothing bridges its environment into that tree, and against the runtime root when something does. `node_modules` is symlinked into a collect or review tree, so a vitest resolved against the runtime root is the same installation the tree imports from; no such bridge exists for a Python environment, so a pytest resolved against the runtime root is the real repository's interpreter pointed at the tree's source — which enumerates the wrong project, or, where that repository has the project installed editable, greenly certifies code that is not the candidate's. Every materialized tree a runner executes in is provisioned with the deployment's `provision_argv` before any private byte is written to it, so nothing provisioning writes, reads, or reports in an error can carry test bytes. Where a deployment declares no `provision_argv` there is no tree environment to prefer and the runtime root is the only one there is.

A measurement that completed is never reported as a failure of the thing it measured. A collect that enumerated and then would not exit has answered the only question collection asks, and refusal is reserved for an empty listing; a bounded runner invocation gives its child a session of its own and kills the process group, so a runner that will not exit cannot outlive the measurement or hold the module cache the next lane needs. An authored gate argv is rebuilt in place and never re-partitioned: a token's position is what binds an option to its value, and a harness that reorders argv hands a runner a different command than the plan declared.

Each started role owns one stable role-scoped checkout or private tree for as long as its pane/session is retained and keeps that pane/session memory across `REVISE` loops, amendments, and scheduler restarts. Mutable role trees are transport scratch only; `lane_state.stage` and immutable artifacts remain workflow authority. Private tester and tester-reviewer trees remain outside the product repository and its Git object database. Builder and code-reviewer do not share a dirty checkout. Private tester bytes never enter builder or code-reviewer paths or prompts.

Herdr topology: lanes are linked children of the Space Herdr reports as the target repository's source checkout. Herdr binds one Space per repository — the first opened on its primary checkout — and names it as `source.source_workspace_id` in `herdr worktree list --cwd <target-repository-root>`. That Space is the operator's own when they have the repository open, which is the normal case; it is unique by construction, so there is no second candidate to disambiguate and no run-scoped parent Space. Maestro adopts it by that binding alone and never tags, renames, focuses, or closes a Space it did not create.

A Space's repository binding is read from `herdr worktree list` and never from `WorkspaceInfo.worktree`. That field is not the binding: Herdr populates it only for a Space it binds when it creates it, and never backfills, so an operator's Space reports none while Herdr simultaneously names it the source. Reading the field answers "unbound" for precisely the Spaces a run must adopt.

Maestro creates a parent only when Herdr reports no source Space for the repository, labelled `<repository-basename>-<first-four-run-hash-characters>` (strip a leading `run-` before the four-character suffix; preserve basename casing). The full `run_id` is runtime identity; the short form is display-only. After `herdr workspace create --cwd <target-repository-root>` the binding is re-proved from `worktree list`: a created parent Herdr does not name as the source — a repository that already has one hands a second Space no binding at all — is closed and refuses `RUN_WORKSPACE_UNBOUND`. A repository path Herdr cannot resolve refuses before any Space exists.

One linked child worktree workspace per active lane, labelled the exact authored `lane_id`, created only when that lane first dispatches, by `herdr worktree open --workspace <parent-workspace-id> --path <approved-lane-role-worktree> --label <lane-id> --no-focus`. Do not simulate this with ordinary tabs, Agent-view filters, display metadata, naming prefixes, or `~/.config/herdr` changes. Role panes are lazy and labelled exactly `tester`, `tester-reviewer`, `builder`, `code-reviewer`, or `integration-reviewer`. The first role in a lane may use the child root pane; later roles split only inside that same child. Never `--current`. Never steal focus. Concurrent first launches resolve exactly one parent and create exactly one child per lane. Final `integration-reviewer` is run-wide but launches in the last topological integration-order lane child when `_final_review` starts. Separate reviewer panes return actionable redacted feedback to the existing implementation role agent; `REVISE` resubmits to that existing session/cwd. Idle after output is normal and is not completion authority. Cleanup begins only after successful `MAIN_PUBLICATION` and derived COMPLETE: idle proof, `/rename <repository-basename>-<short-run>-<lane-id>-<role>` as composer text plus Enter, exact `Session renamed to "<full-session-name>".` confirmation, close the lane children, then cwd removal. A parent Maestro did not create is never closed; closing a parent Space cascade-closes its linked children, so closing the operator's Space would take their own work with it. Rename or close failure leaves panes and cwd intact without rolling back publication. Cleanup is idempotent.

Scheduler restart first rediscovers candidates from Herdr report-metadata (Maestro-owned source: full `run_id`, canonical repository fingerprint, `lane_id` where applicable, role where applicable, parent run workspace ID for lane children) and adopts only an exact metadata identity whose live IDs, parent relationship, pane cwd, agent ownership, stable agent name, and role label all verify. It live-checks the process and resubmits the current stage from its immutable input. Only confirmed `pane_not_found`, `workspace_not_found`, `agent_not_found`, or process death permits recreation of the missing expected object. Empty labels, label-only matches, legacy stage/attempt panes, malformed observations, unreachable Herdr, mismatched placement, wrong-parent, dirty/out-of-scope trees, duplicates, and unproved agents are refused, never adopted or renamed as current roles. Transport identity stays out of `lane_state` and artifact identity. Never adopt a mutable branch ref, envelope, durable `actor_sessions`/generation row, or filesystem state as workflow authority.

If the scheduler process dies before the stage-specific durable boundary, resume resubmits the current stage from its last immutable input to the proved persistent role agent without replacing its role tree or session. If it dies after the transaction commits, resume reads the advanced stage and does not rerun the completed stage. Mutable role files may survive as transport scratch but cannot advance or reconstruct workflow state.

---

## 11. Private-test boundary

Reuse `hidden_vault.py` and its object-database isolation.

Untyped private tester files are independent hidden meta-tests. They must not collide with declared builder/product outputs. An authored `lane_kind=tests` tester authors private acceptance files exactly at that lane's declared output paths; the returned private-file set must equal those outputs before `TEST_DRAFT`.

- Test author and test reviewer can access the private draft repository. The test reviewer receives the tests lane spec and private overlay.
- New `TEST_DRAFT` and `SEALED_TEST_BUNDLE` artifacts bind hidden files by `private-manifest.v1` digest without publishing paths. Legacy drafts without that marker safely filter to the declared-output fallback.
- On `PASS`, seal the accepted bundle into the vault and record only digest/reference plus public behavioral contract.
- A typed builder gets predecessor bundle ID and digest plus its own product contract. It never receives private source, vault path, or tests-lane paths.
- Code reviewer receives the candidate commit and controlled vault access, runs the predecessor sealed suite in scratch, and emits only verdict, public result summary, and redacted findings.
- On an untyped lane, a candidate collision raises typed `PRIVATE_PATH_COLLISION`. `REVIEWING_CODE` durably emits `TEST_INVALIDATION`, atomically resets the lane to `WRITING_TESTS`, passes the redacted actionable reason to the persistent tester, and leaves prior artifacts as immutable history. Other `IsolationError` failures fail closed. Typed build review overlays the predecessor suite even when those paths exist in the candidate.
- Private objects must be absent from the run repo, builder worktree, refs, rev-list, and fetch paths.

---

## 12. Integration merge and publication

Every Git-mutating operation shares one lock order: (1) run mutation lock, (2) integration-ref lock, (3) exclusive target-worktree maintenance lock, keyed by the recorded per-worktree Git-dir identity. Never acquire in another order. Never acquire an OS lock while holding a SQLite transaction. Lane merge takes locks 1–2. Final review takes locks 1–2. `apply_amendment` acquires locks 1–3, reconciles any existing publication receipt, refuses if publication has occurred, then performs its atomic projection/stage reset before releasing the locks. Publication holds locks 1–3 through the Git receipt/`main` CAS. Failure to acquire lock 3 refuses `PUBLICATION_WORKTREE_LOCK_REFUSED`.

For a changing merge, compute the expected tree with `git merge-tree --write-tree` in the bound product repository. Conflict or nonzero output refuses without moving the ref. CAS the integration ref from `before_sha` to the constructed `after_sha`. Never merge a mutable candidate branch.

Zero-delta revalidation records `before_sha == candidate_sha == after_sha` with `revalidated=true` and creates no Git commit. If `BUILDER_OUTPUT.changed=false` and its base SHA is no longer the integration HEAD, append `BASE_INVALIDATION` and return to `BUILDING`.

Final reviewer evaluates the exact integration HEAD against the active plan revision, ordered public contracts, sealed lane tests, and architecture constraints named by the fingerprint.

Publication is exactly-once and receipt-backed: immutable `refs/maestro/publications/<run-id>/<review-input-fingerprint>` plus `MAIN_PUBLICATION`. `main` reaching the same SHA without Maestro's receipt is external activity and refuses `PUBLICATION_EXTERNAL_MISMATCH`, never inferred as successful publication.

A crash after Git mutation but before ledger commit is idempotently reconciled from exact SHA/receipt evidence without adopting process state, including a present `locks/legacy_integration_retarget.<run-id>.json` journal.

---

## 13. Operator commands

Frozen operator surface:

```text
uv run adws/maestro.py run start <approved-plan> --repo <target-worktree-root> --main-ref <ref>
uv run adws/maestro.py run resume <run-id>
uv run adws/maestro.py run amend <approved-plan> --run <run-id>
uv run adws/maestro.py run status <run-id>

```

- `run start` creates the run, initial plan revision, complete DAG projection, and initial `PLANNED` lane states in one transaction after pinning `integration_initial_sha` from the plan's declared integration branch, then creates the integration ref from zero to that SHA.
- `run resume` continues the next incomplete stage from the last accepted immutable artifact. After an explicit `PAUSE`, it restores the recorded stage/input. After `AMENDMENT_REQUIRED`, it leaves the lane waiting.
- `run amend <approved-plan> --run <run-id>` is the only verb that may apply a `PLAN_AMENDMENT`.
- `run status` derives §6 from durable rows after revalidating `runtime_state_fingerprint`.

`run start` after ledger registration and `run resume` after binding detach the configured Next.js dashboard launcher. The scheduler is never blocked. `run amend` and `run status` do not launch.

An explicit pause records `USER_WAIT` as specified in §5. Publication after a passing final review is the runtime's exactly-once receipt step, not a second stage enum.

Template-source invocation of run creation refuses `RUN_REPOSITORY_MISMATCH`.

---

## 14. Legacy ledger refusal

Opening a legacy ledger for execution fails with `LEDGER_SCHEMA_UNSUPPORTED` and instructions to preserve it read-only and start a new run/database.

No old run is mapped from prior state, phase, candidate, or generation rows. Status/export may remain as a separate read-only utility only if already present; it must not feed execution.

---

## 15. Removed authorities (historical)

Source cutover deleted these mechanisms. They are not in the current factory and must not be reintroduced. Former owners are recorded so leftover vocabulary in older docs or logs is recognizable as removed history, not live contract.

| Item | Owner | Why deleted |
|---|---|---|
| `NodeState` dual enum | `adw_modules/scheduler_types.py` | second representation of lane position |
| `LanePhase` dual enum and `LANE_PHASE_TERMINAL` | `scheduler_types.py` | second representation of lane position |
| `_guard_transition` / `_EXITS` legality model, `ABSOLUTELY_TERMINAL`, `TERMINAL_WITHOUT_MERGE`, `IllegalTransition` as that model's API | `adw_modules/lifecycle.py`, `scheduler_types.py` | legality not derived from the nine-stage table |
| `NodeLifecycle`, `set_lane_phase` | `lifecycle.py`, `scheduler_types.py` | duplicate mutable lane authority |
| `PendingCause`, `pending_cause_label` | `scheduler_types.py`, `lifecycle.py` | second encoding of why a lane is not advancing |
| `MergeCause`, `merge_cause_label`, `MERGE_CAUSE_UNRECORDED` | `scheduler_types.py`, `lifecycle.py` | merge authority besides `INTEGRATION_MERGE` |
| Attempts as durable authority: `AttemptRecord`, `AttemptIdentity`, `get_attempt`, `attempts_for`, attempt numbering | `lifecycle.py`, `attempt_identity.py`, `scheduler.py` | attempt identity is not stage |
| Salvage, late envelopes, sealed-output recovery, dirty-worktree recovery | `salvage.py`, `lifecycle.py`, `scheduler.py` | resume is stage restart from immutable input |
| `lane_candidates`, `candidate_reviews`, `repair_handoffs`, candidate sequence/parent identity | lifecycle/scheduler stores | candidate SHA lives only on `BUILDER_OUTPUT` and Git refs |
| Durable `actor_sessions`, reviewer/builder generations, generation fences, reviewer occupancy as acceptance, unproved live-agent/pane/process/dirty-worktree adoption | `scheduler.py`, `maestro.py`, lifecycle | transport facts are logs, not authority; reconnect is identity-proved Herdr topology, not a durable session table |
| `WorkspaceInfo.worktree` as a Space's repository binding; the `workspace list` scan for bound Spaces; the `DUPLICATE_RUN_WORKSPACE` refusal; a run-scoped parent Space | `adw_modules/launcher.py` | Herdr sets that field only for a Space it binds at creation and never backfills, so every operator Space read as unbound; the binding is `worktree list`, which makes the source Space unique and leaves no duplicate to name |
| Recovery markers and one-shot marker consumption | lifecycle/scheduler | consumed markers are not restart inputs |
| Semantic/review spend ceilings, floors, grants | `maestro.config.yaml` `execution.*_ceiling`, `retry_policy.py` | they prevent explicit user continuation |
| CLI verbs `retry`, `skip`, `abandon` and their handlers | `maestro.py` `build_parser` | escape verbs outside the frozen table |
| `attempt salvage` | `maestro.py` | dirty/live recovery |
| Synthetic `::review` DAG nodes and review-node dependency rewrites | plan compiler / scheduler | review is a lane stage |
| Generic produced-symbol reachability hard gate and `PRODUCED_SYMBOL_UNREFERENCED` admission | plan compiler / verification | not an objective compiler check |
| Coordinator workspace leases and repository-state authority that duplicate lane/run artifacts | `coordinator.py`, `coordinator_store.py` | one lane-stage authority |
| Compatibility aliases, legacy review migration, deprecated columns, no-op wrappers, `_migrate` of in-flight ledgers | lifecycle store | no guessed migration |
| Same-session correction promised as lifecycle continuation; uncommitted worktree state preserved across death as resume input | `agents.py` / README historical prose | death restarts from last immutable artifact; role pane continuity is transport only |

Vendor/model/route, pane/session identifiers, process exit details, and transport timing may remain in logs. They cannot participate in stage transitions, identity, resume, or acceptance.

---

## 16. Acceptance-to-transition map

Every required scenario is proved by named transitions. A transition without a scenario is forbidden; a scenario without a transition is unsatisfied.

| Required scenario | Proving transitions |
|---|---|
| Private test-author / test-reviewer revision loop | `PLANNED → WRITING_TESTS` (`LANE_PLAN`); `WRITING_TESTS → REVIEWING_TESTS` (`TEST_DRAFT`); `REVIEWING_TESTS` `REVISE` → `WRITING_TESTS`; `REVIEWING_TESTS` `PASS` → `TESTS_SEALED` |
| Accepted tests are sealed and hidden from implementation builders | Untyped `TESTS_SEALED → BUILDING`; authored `lane_kind=tests` `TESTS_SEALED → MERGED`; `SEALED_TEST_BUNDLE` contains vault digest/reference while private bytes stay absent from run repo and builder worktree; a typed build receives only the predecessor bundle ID/digest |
| Private tester collision with a candidate product path invalidates the sealed tests | `REVIEWING_CODE` → `WRITING_TESTS` (`TEST_INVALIDATION`); redacted reason to the persistent tester; prior artifacts retained; other `IsolationError` fail closed |
| Builder / code-reviewer revision loop with actionable redacted feedback | `BUILDING → REVIEWING_CODE` (`BUILDER_OUTPUT`); `REVIEWING_CODE` `REVISE` → `BUILDING` (`CODE_REVISE` input); `REVIEWING_CODE` `PASS` → `READY_TO_MERGE` |
| Byte-identical builder revision after `CODE_REVIEW(REVISE)` is refused | lane stays `BUILDING`; `NOOP_BUILDER_REVISION` before a new `BUILDER_OUTPUT`; review retained; reviewer not relaunched |
| Each accepted build or untyped lane merges exactly once into the integration branch | `READY_TO_MERGE → MERGED` (`INTEGRATION_MERGE`); completion-key uniqueness; changing merge CAS; zero-delta revalidation creates no second commit. An authored tests lane becomes `MERGED` at seal and emits no `INTEGRATION_MERGE` |
| Dependent lane execution uses the typed predecessor receipt | A tests-kind dependency supplies its current-revision `SEALED_TEST_BUNDLE`; a build-kind or untyped dependency supplies its current-revision `INTEGRATION_MERGE`. The ready predicate also requires every `needs` lane to be `MERGED` |
| All completed lanes receive final integration review before publication to main | derived status “integration review pending”; `complete_final_review`; `PASS` then `complete_publication`; `REVISE` → named lanes `WAITING_FOR_USER` |
| Interrupted work resumes from its last immutable completed-stage artifact | death before `complete_stage` commit: rediscover the proved persistent role, live-check it, and resubmit the current stage from the last immutable input without replacing its role tree/session; confirmed dead or typed `agent_not_found`: recreate the role; death after commit: read advanced stage; refuse legacy stage/attempt, malformed, unreachable, mismatched, dirty-boundary, or unproved observations |
| User amendment: a changed lane invalidates every former input and restarts its changed projection at `PLANNED`; `AMENDMENT_REQUIRED` named lanes are already `MERGED` and go `WAITING_FOR_USER` → `PLANNED` on `run amend` with changed `spec_digest`; unchanged unstarted dependents keep `PLANNED`/`WRITING_TESTS`/`REVIEWING_TESTS`/`TESTS_SEALED`; unchanged authored `build` dependents at `BUILDING`/`REVIEWING_CODE`/`READY_TO_MERGE`/`MERGED` revalidate from `BUILDING`; unchanged untyped started/merged lanes and unchanged already-`MERGED` authored `tests` lanes reset to `TESTS_SEALED`; independently `PAUSE`-waiting lanes stay `WAITING_FOR_USER` | `apply_amendment` edges in §3 and policy in §5; `PAUSE` waits remain explicit; `AMENDMENT_REQUIRED` ignores bare resume |
| Stale integration base on a zero-delta candidate does not publish a dummy commit | `READY_TO_MERGE` → `BUILDING` via `BASE_INVALIDATION` |
| Legacy execution is refused | open ledger → `LEDGER_SCHEMA_UNSUPPORTED` |
| Template-source run creation is refused | `run start` from Maestro/the-library/template tree → `RUN_REPOSITORY_MISMATCH` |

Two-lane vertical slice: untyped lane A has no `needs`; lane B `needs` A. Independent author/review/build of A may proceed while B waits on `MERGED`. B's first `BUILDING` input includes A's `INTEGRATION_MERGE`. Final review runs only after both are `MERGED`. A typed tests→build pair: the tests lane is `MERGED` after `SEALED_TEST_BUNDLE`; the build lane's first `BUILDING` input includes that current-revision sealed bundle, not an `INTEGRATION_MERGE`.

---

## 17. Paths

| Path | Role |
|---|---|
| `MAESTRO_architecture.md` | this contract |
| `README.md` | operator-facing contract |
| `maestro_prompt.md` | agent-facing contract |
| `.claude/skills/sssf/templates/adws/` | development template source (not a run host) |
| `.claude/skills/sssf/templates/adws/maestro.py` | template copy of the frozen CLI (not a run host) |
| `.claude/skills/sssf/templates/adws/maestro.config.yaml` | stamped deployment config; must require absolute `runtime_state_root` |
| `.claude/skills/sssf/templates/adws/adw_modules/hidden_vault.py` | private vault isolation (retained) |
| `.claude/skills/sssf/scripts/install.py` | stamps the template into a product repo as `adws/` |
| `adws/maestro.py` | deployment CLI |
| `adws/maestro.config.yaml` | deployment config |
| `skills/sssf/templates/adws/` | the-library mirror of the template |
| `.claude/skills/sssf/apps/dashboard/bin/maestro-dashboard` | detached Next.js dashboard launcher (observability only) |

Herdr and OMP are transport for agent dispatch. Pane text, process liveness, idle status, and session directories are not workflow authority.
