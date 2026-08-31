# Maestro

Artifact-driven software factory. One approved plan becomes a dependency DAG. Each lane has exactly one persisted stage. Agents do bounded work inside a stage. Code commits the immutable artifact and the next stage together.

This repository packages that factory as the `sssf` skill under `.claude/skills/sssf/`. Operator execution is from a **stamped deployment** (`adws/maestro.py`), never from the template tree.

Authoritative contract: [`MAESTRO_architecture.md`](MAESTRO_architecture.md). Agent contract: [`maestro_prompt.md`](maestro_prompt.md).

Rendered docs: [`docs/index.html`](docs/index.html). Current architecture diagram: [`docs/architecture/00-artifact-factory.html`](docs/architecture/00-artifact-factory.html).

---

## Product flow

1. `deep-interview`, `arch-brownfield`, `planf3`, and `arch-review` produce one approved executable plan revision.
2. The plan compiler checks only objective properties (schema, DAG, ownership, acceptance, integration order).
3. Every ready lane runs private test author → test review → test sealing → builder → code review.
4. `REVISE` returns to the author or builder with actionable, redacted feedback. `PASS` advances the lane.
5. An accepted lane merges exactly once into the run's integration branch.
6. A dependent lane starts from the integration commit that already contains every merged dependency.
7. When every lane is merged, a final reviewer evaluates that integration commit with all sealed tests. `PASS` publishes that SHA to `main` exactly once, receipt-backed. `REVISE` waits for a user amendment.
8. Process death restarts the current incomplete stage from its last immutable input. A still-running role pane may reconnect as transport by proved identity. Unknown, mismatched, or dirty worktrees and unproved agents are refused.

---

## Lane stages

The persisted lane stage is the sole durable workflow authority. Exactly these nine names:

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

`REVISE` is review-artifact data, not a tenth stage. Reviewers are stages of the lane, not extra DAG nodes.

---

## Monitoring topology

One Herdr workspace per project+run (project identity plus run ID). One tab per lane. Every lane tab contains exactly five sibling panes named `tester`, `test-reviewer`, `builder`, `code-reviewer`, and `integration-reviewer`. All five role agents persist for the run. Reviewer panes return actionable redacted feedback to the existing implementation role agent. Idle after output is normal and is not completion.

Each role owns one stable role-scoped checkout or private tree and keeps its pane/session memory across review `REVISE` loops and scheduler restarts. The mutable role tree is transport scratch, never workflow authority; `lane_state.stage` plus immutable artifacts remain the only durable workflow authority.

Role confinement is mandatory, not prompt guidance. OMP and Claude remain host processes so Herdr can display and control them and their existing OAuth profiles can authenticate normally. Both receive only the Bash tool. A fail-closed hook rewrites every model-issued command into a disposable OrbStack/Docker container with no network, a read-only container root, scrubbed credentials, hidden Git metadata, checkout-local scratch, and only that role tree mounted writable. The image contains project runtimes, not coders or OAuth material. Missing Docker, image, hook, or container support refuses launch.

Scheduler restart first rediscovers the five stable agents by deterministic project/run/lane/role identity and typed Herdr workspace/tab/pane plus canonical role-scoped cwd. It live-checks the process and resubmits the current stage from its immutable input. Only a confirmed dead or `agent_not_found` role is recreated. Empty labels, legacy stage/attempt panes, malformed observations, unreachable Herdr, mismatched placement, and unproved agents are refused, never adopted or renamed as current roles.

---

## Plan compiler

Admits a plan only when all of these hold:

- Schema and required fields.
- Existing dependency IDs and an acyclic DAG.
- Declared outputs are exact normalized repository-relative POSIX file paths (not directories or globs).
- No absolute, empty, `.`, or `..` path components.
- No duplicate, equal, ancestor, or descendant ownership conflicts across lanes.
- Public acceptance criteria per lane.
- Deterministic integration order from the DAG.

The compiler does not apply generic semantic or produced-symbol reachability gates.

---

## Operator commands

Run these from a **deployment** whose canonical Git common directory equals the `--repo` worktree's canonical Git common directory. Invoking `run start` from this Maestro checkout, the-library, or any other template source refuses `RUN_REPOSITORY_MISMATCH`.

```bash
uv run adws/maestro.py run start <approved-plan> --repo <target-worktree-root> --main-ref <ref>
uv run adws/maestro.py run resume <run-id>
uv run adws/maestro.py run amend <approved-plan> --run <run-id>
uv run adws/maestro.py run status <run-id>
```

| Verb | Effect |
|---|---|
| `run start` | Bind one dedicated publication worktree and main ref, record `runtime_state_root`, insert the run and initial `PLANNED` lanes, create `refs/maestro/integration/<run-id>` from zero (`INTEGRATION_REF_COLLISION` if that ref exists at another SHA) |
| `run resume` | Continue the next incomplete stage from the last accepted immutable artifact. Restores an explicit `PAUSE`. Does **not** resolve `AMENDMENT_REQUIRED` waits |
| `run amend` | `run amend <approved-plan> --run <run-id>` applies a `PLAN_AMENDMENT`. Required after final-review `REVISE`. Named lanes are already `MERGED`, so `needs`/output changes are refused; the amendment must change `spec_digest` of every named lane (hence `lane_projection_digest`) or refuse `AMENDMENT_DOES_NOT_ADDRESS_REVIEW`. Those named lanes restart at `PLANNED` |
| `run status` | Derived run status from durable rows (complete / waiting / executing / integration review pending / publishable). Not a second lane-stage enum |

`adws/maestro.config.yaml` must declare one absolute `runtime_state_root` directory, mode `0700`, outside the target repository and both template checkouts. `runtime_state_fingerprint` is SHA-256 over canonical realpath, device, and inode. Every start, resume, amend, status, and publication revalidates that fingerprint.

`--repo` is the dedicated non-bare publication worktree, distinct from implementation-agent worktrees and from Maestro/the-library template trees. Implementation agents never edit it. `HEAD` must be the configured main ref at start. Recorded equalities: `integration_initial_sha == target_initial_main_sha == <resolved target_main_ref SHA>`.

Immutable Git identities (not stage):

- candidate: `refs/maestro/candidates/<run-id>/<lane-id>/<input-digest>` on each `BUILDER_OUTPUT`
- publication: `refs/maestro/publications/<run-id>/<review-input-fingerprint>` plus `MAIN_PUBLICATION`

The final-review input fingerprint is SHA-256 of canonical JSON (`schema_version` 1) containing the integration SHA, active plan revision and digest, and ordered lanes `{lane_id, spec_digest, public_contract_artifact_id, sealed_test_bundle_artifact_id}`. Observed target-main SHA is publication's expected-before value, not review identity. `main` matching that SHA without Maestro's receipt refuses `PUBLICATION_EXTERNAL_MISMATCH`. Failure to acquire the target-worktree lock refuses `PUBLICATION_WORKTREE_LOCK_REFUSED`. A later stage using an invalidated input refuses `STALE_STAGE_INPUT`.

### Pause and amendment

An explicit pause records `USER_WAIT` (`wait_reason=PAUSE`) with the suspended stage and complete immutable input, and moves the lane to `WAITING_FOR_USER`. `run resume` then restores that stage/input.

A final-review `REVISE` records `USER_WAIT` (`wait_reason=AMENDMENT_REQUIRED`). Bare resume leaves it waiting. Only `run amend` continues, and every named lane goes to `PLANNED`.

Changed lanes restart at `PLANNED`. Unchanged dependents at `BUILDING`, `REVIEWING_CODE`, or `READY_TO_MERGE`, and unchanged already-`MERGED` dependents, revalidate from `BUILDING`. Unchanged unstarted dependents (`PLANNED`, `WRITING_TESTS`, `REVIEWING_TESTS`, `TESTS_SEALED`) keep their stage. Independently paused lanes stay `WAITING_FOR_USER` until an explicit resume after dependencies re-merge. Published runs are immutable.

### Crash

Death before the stage transaction commits: recreate the current stage from its last immutable input. Death after commit: read the advanced stage. Do not resurrect a dead agent, consume recovery markers, or keep uncommitted worktree state as resume input. A still-running role pane may reconnect only by proved identity.

### Legacy ledgers

Opening a previous-schema ledger for execution fails with `LEDGER_SCHEMA_UNSUPPORTED`. Preserve it read-only and start a new run/database. There is no guessed stage mapping.

---

## Private tests

Test author and test reviewer share a private draft. On `PASS`, the bundle is sealed into the vault. The builder sees the public contract and sealed digest only — not private source, fixtures, selectors, expected literals, or vault paths. Code review may run the sealed tests and must redact findings before they return to the builder.

---

## Install

Stamp the factory into a **product** repository with the existing skill installer, then run Maestro from that deployment (`adws/maestro.py`).

**Prereqs:** [`uv`](https://docs.astral.sh/uv/), `omp`, `herdr`, `sqlite3`, and authentication for the routes in the stamped roster.

### Agentic install

In the product repository, type `/sssf install` inside Claude Code. The agent follows `.claude/skills/sssf/cookbooks/install.md`.

### Manual install

From the product repository root, with the skill already present at `.claude/skills/sssf/`:

```bash
uv run .claude/skills/sssf/scripts/install.py
```

What lands:

| In the product repo | Source under `.claude/skills/sssf/` |
|---|---|
| `adws/maestro.py`, `adws/maestro.config.yaml` | `templates/adws/` |
| `adws/adw_modules/` | `templates/adws/adw_modules/` |
| `adws/adw_sssf_config/sssf.config.yaml` | `templates/sssf.config.yaml` |
| `adws/adw_data/prompt_engineering/` | `templates/prompt_engineering/` |
| `.env.sample` | `templates/env.sample` |

Edit `adws/maestro.config.yaml` so `runtime_state_root` is an existing absolute mode-`0700` directory. Then create a dedicated publication worktree whose `HEAD` is the main ref you will pass to `run start`.

The visualizer at `.claude/skills/sssf/apps/visualizer/` remains a read-only SQLite UI. It is not workflow authority.

---

## What this factory refuses

- Second encodings of stage or identity (parallel state/phase enums, attempt numbers, candidate sequences, generations, pending causes).
- Unproved adoption of agents, panes, sessions, or dirty worktrees; durable `actor_sessions` or actor generations; pane occupancy as acceptance.
- Spend ceilings, floors, or grants that block explicit user continuation.
- Escape verbs `retry`, `skip`, and `abandon`.
- Generic semantic or produced-symbol reachability as a hard admission gate.
- More than one merge of an accepted lane artifact.
- Guessed migration of in-flight legacy ledgers.
- Builder access to private test bytes.

Transport details (Herdr pane ids, OMP profiles, process exits) may be logged. They cannot advance a lane.

---

## License

MIT, see [`LICENSE`](LICENSE).
