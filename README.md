# Maestro

Artifact-driven software factory. One approved plan becomes a dependency DAG. Each lane has exactly one persisted stage. Agents do bounded work inside a stage. Code commits the immutable artifact and the next stage together.

This repository packages that factory as the `sssf` skill under `.claude/skills/sssf/`. Operator execution is from a **stamped deployment** (`adws/maestro.py`), never from the template tree.

Authoritative contract: [`MAESTRO_architecture.md`](MAESTRO_architecture.md). Agent contract: [`maestro_prompt.md`](maestro_prompt.md).

Rendered docs: [`docs/index.html`](docs/index.html). Current architecture diagram: [`docs/architecture/00-artifact-factory.html`](docs/architecture/00-artifact-factory.html).

---

## Product flow

1. `deep-interview`, `arch-brownfield`, `planf3`, and `arch-review` produce one approved executable plan revision.
2. The plan compiler checks only objective properties (schema, DAG, ownership, acceptance, integration order).
3. Untyped lanes run private test author → test review → test sealing → builder → code review. Authored `lane_kind=tests` routes `PLANNED → WRITING_TESTS → REVIEWING_TESTS → TESTS_SEALED → MERGED` and emits `SEALED_TEST_BUNDLE` without builder, code review, or integration merge. Authored `lane_kind=build` routes `PLANNED → BUILDING → REVIEWING_CODE → READY_TO_MERGE → MERGED`, skipping test author/reviewer. Absent `lane_kind` keeps the universal lifecycle.
4. `REVISE` returns to the author or builder with actionable, redacted feedback. `PASS` advances the lane. If `CODE_REVIEW(REVISE)` exists and the next declared-output tree is byte-identical to the prior candidate, the factory refuses `NOOP_BUILDER_REVISION` before a new `BUILDER_OUTPUT`; the lane stays `BUILDING` and the review is retained.
5. An accepted build or untyped lane merges exactly once into the run's integration branch. A typed tests lane is `MERGED` after seal; dependents use that current-revision sealed bundle as readiness. Build/untyped dependencies use the current-revision integration merge receipt.
6. A dependent lane starts when every predecessor is `MERGED`.
7. When every lane is merged, a final `integration-reviewer` launches lazily in the last topological integration-order lane's child workspace (not `lanes[0]`) and evaluates that integration commit with all sealed tests. `PASS` publishes that SHA to `main` exactly once, receipt-backed. `REVISE` retains every affected lane's role sessions and waits for a user amendment. Cleanup does not run on `MERGED`.
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

## Herdr runtime topology

One parent run Space group per (repository identity, `run_id`). Display label is `<repository-basename>-<first-four-run-hash-characters>`: strip a leading `run-` before selecting the four-character suffix; preserve the repository basename's casing. The full `run_id` is runtime identity; the short form is display-only. Examples: FDAdb + `e892fe...` → `FDAdb-e892`; FDAdb + `run-9f20c17f...` → `FDAdb-9f20`.

One linked child worktree workspace per active lane, labelled the exact authored `lane_id` (for example `lane-wp6-tests`), created only when that lane first dispatches an agent. The Spaces sidebar hierarchy comes from Herdr's native worktree Space-group relationship:

```text
herdr workspace create ...
herdr worktree open --workspace <parent-run-workspace-id> \
  --path <approved-lane-role-worktree> --label <lane-id> --no-focus
```

Do not simulate this with ordinary tabs, Agent-view filters, display metadata alone, naming prefixes, or changes to `~/.config/herdr`.

Role panes are created only when that role first starts. Live pane labels are exactly `tester`, `tester-reviewer`, `builder`, `code-reviewer`, or `integration-reviewer` — do not repeat the run or lane. The first role in a lane may use the child workspace's root pane; later roles split only inside that same child. Never split from `--current` or another lane's pane. All placement uses explicit parent, child, tab, or pane IDs and `--no-focus`. Concurrent first launches create exactly one parent and exactly one child per lane. No pane from one run may land in another run's parent or child workspace.

Stable actor identity is `(repository identity, run_id, lane_id, role)`. A reviewer `REVISE` leaves both the originating role and reviewer panes open; the next correction resubmits to the existing session, pane, agent, transcript, and approved cwd with a fresh prompt and envelope. Each of tester, tester-reviewer, builder, and code-reviewer owns a distinct role-scoped worktree; builder and code-reviewer do not share a dirty checkout; private tester bytes never enter builder or code-reviewer paths or prompts. The mutable role tree is transport scratch, never workflow authority; `lane_state.stage` plus immutable artifacts remain the only durable workflow authority. Idle after output is normal and is not completion.

Role confinement is mandatory, not prompt guidance. OMP and Claude remain host processes so Herdr can display and control them and their existing OAuth profiles can authenticate normally. Both receive only the Bash tool. A fail-closed hook rewrites every model-issued command into a disposable OrbStack/Docker container with no network, a read-only container root, scrubbed credentials, hidden Git metadata, checkout-local scratch, and only that role tree mounted writable. The image contains project runtimes, not coders or OAuth material. Missing Docker, image, hook, or container support refuses launch.

Scheduler restart and amendment rediscover from Herdr report-metadata (Maestro-owned source: full `run_id`, canonical repository fingerprint, `lane_id` where applicable, role where applicable, parent run workspace ID for lane children) plus verified live IDs, parent relationship, pane cwd, agent ownership, stable agent name, and role label. Labels are display text, not adoption proof. Only a confirmed `pane_not_found`, `workspace_not_found`, or `agent_not_found` recreates the missing expected object. Duplicate, label-only, wrong-parent, wrong-cwd, wrong-agent, dirty, unknown, or malformed candidates refuse; never adopt or rename them as current roles. Transport identity stays out of `lane_state` and artifact identity. Amendment reuses the same parent and expected role sessions; new lanes create linked children lazily.

Keep role panes open while the run is executing, waiting, integration-review pending, publishable, or blocked by review/amendment. Cleanup may begin only after successful `MAIN_PUBLICATION` and derived COMPLETE. For every role: prove idle, send `/rename <repository-basename>-<short-run>-<lane-id>-<role>` as composer text plus Enter, wait for exact `Session renamed to "<full-session-name>".`, then close child lane workspaces, then the parent if it has no retained run-level work, then remove cwd. Rename or close failure leaves that pane and cwd intact (for example `SESSION_RENAME_UNCONFIRMED`) without rolling back publication. Cleanup is idempotent.

---

## Plan compiler

Admits a plan only when all of these hold:

- Schema and required fields.
- Optional `lane_kind` is `tests` or `build`; absent keeps the universal lifecycle. Projection digest includes kind only when authored.
- An authored `build` lane has exactly one `tests` dependency and may also depend on `build` lanes.
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
| `run start` | Bind one dedicated publication worktree and main ref, pin `integration_initial_sha` from the plan's declared integration branch, record `runtime_state_root`, insert the run and initial `PLANNED` lanes, create `refs/maestro/integration/<run-id>` from zero to that SHA (`INTEGRATION_REF_COLLISION` if that ref exists at another SHA) |
| `run resume` | Continue the next incomplete stage from the last accepted immutable artifact. Restores an explicit `PAUSE`. Does **not** resolve `AMENDMENT_REQUIRED` waits |
| `run amend` | `run amend <approved-plan> --run <run-id>` applies a `PLAN_AMENDMENT`. Required after final-review `REVISE`. Named lanes are already `MERGED`, so `needs`/output changes are refused; the amendment must change `spec_digest` of every named lane (hence `lane_projection_digest`) or refuse `AMENDMENT_DOES_NOT_ADDRESS_REVIEW`. Those named lanes restart at `PLANNED` |
| `run status` | Derived run status from durable rows (complete / waiting / executing / integration review pending / publishable). Not a second lane-stage enum |

`run start` after ledger registration and `run resume` after binding detach the configured Next.js dashboard launcher. The scheduler is never blocked. `run amend` and `run status` do not launch.

`adws/maestro.config.yaml` must declare one absolute `runtime_state_root` directory, mode `0700`, outside the target repository and both template checkouts. `runtime_state_fingerprint` is SHA-256 over canonical realpath, device, and inode. Every start, resume, amend, status, and publication revalidates that fingerprint.

New installations stamp:

```yaml
dashboard:
  enabled: true
  launcher: <absolute path from the installed skill>
  api_port: 4600
  ui_port: 4317
  open: true
```

Old, missing, or invalid dashboard config warns and fails open. The launcher reuses only owned processes, never kills unknown port owners, ensures the Bun API source list contains the current canonical ledger plus Next `/runs` readiness, then opens `/runs`. API/UI logs and pid ownership live under user cache.

`--repo` is the dedicated non-bare publication worktree, distinct from implementation-agent worktrees and from Maestro/the-library template trees. Implementation agents never edit it. `HEAD` must equal the configured `--main-ref` at start. Every lane spec must declare one consistent safe `spec.integration.integration_branch`. Run creation resolves that branch once and pins its SHA as `integration_initial_sha` and `refs/maestro/integration/<run-id>`. `target_initial_main_sha` remains the separately recorded publication-main SHA and may differ. Tester worktrees and writing-test input identity use the durable run integration tip. Builders never receive private tests.

Immutable Git identities (not stage):

- candidate: `refs/maestro/candidates/<run-id>/<lane-id>/<input-digest>` on each `BUILDER_OUTPUT`
- publication: `refs/maestro/publications/<run-id>/<review-input-fingerprint>` plus `MAIN_PUBLICATION`

The final-review input fingerprint is SHA-256 of canonical JSON (`schema_version` 1) containing the integration SHA, active plan revision and digest, and ordered lanes `{lane_id, spec_digest, public_contract_artifact_id, sealed_test_bundle_artifact_id}`. Observed target-main SHA is publication's expected-before value, not review identity. `main` matching that SHA without Maestro's receipt refuses `PUBLICATION_EXTERNAL_MISMATCH`. Failure to acquire the target-worktree lock refuses `PUBLICATION_WORKTREE_LOCK_REFUSED`. A later stage using an invalidated input refuses `STALE_STAGE_INPUT`.

### Pause and amendment

An explicit pause records `USER_WAIT` (`wait_reason=PAUSE`) with the suspended stage and complete immutable input, and moves the lane to `WAITING_FOR_USER`. `run resume` then restores that stage/input.

A final-review `REVISE` records `USER_WAIT` (`wait_reason=AMENDMENT_REQUIRED`). Bare resume leaves it waiting. Only `run amend` continues, and every named lane goes to `PLANNED`.

Changed lanes restart at `PLANNED` (including a paused lane whose projection changed). Unchanged authored `build` dependents at `BUILDING`, `REVIEWING_CODE`, `READY_TO_MERGE`, or `MERGED` revalidate from `BUILDING`. Unchanged authored `tests` lanes already `MERGED`, and unchanged untyped lanes at `BUILDING`, `REVIEWING_CODE`, `READY_TO_MERGE`, or `MERGED`, reset to `TESTS_SEALED` so the scheduler re-emits a current-revision `SEALED_TEST_BUNDLE`; typed tests then return `MERGED` before builds start, untyped then continue `BUILDING`. Unchanged unstarted dependents (`PLANNED`, `WRITING_TESTS`, `REVIEWING_TESTS`, `TESTS_SEALED`) keep their stage. Unchanged independently paused lanes stay `WAITING_FOR_USER` until an explicit resume after dependencies re-merge. Published runs are immutable.

### Crash

Death before the stage transaction commits: recreate the current stage from its last immutable input. Death after commit: read the advanced stage. Do not resurrect a dead agent, consume recovery markers, or keep uncommitted worktree state as resume input. A still-running role pane may reconnect only by proved identity.

### Legacy ledgers

Opening a previous-schema ledger for execution fails with `LEDGER_SCHEMA_UNSUPPORTED`. Preserve it read-only and start a new run/database. There is no guessed stage mapping.

---

## Private tests

Test author and test reviewer share a private draft. Untyped private tester files are independent hidden meta-tests and must not collide with declared builder/product outputs. Typed `tests` lanes author private acceptance files exactly at declared outputs; the returned set must equal those paths before `TEST_DRAFT`. New `TEST_DRAFT` and `SEALED_TEST_BUNDLE` artifacts bind hidden files by `private-manifest.v1` digest without publishing paths. Legacy drafts without that marker safely filter to the declared-output fallback.

On `PASS`, the bundle is sealed into the vault. A typed builder sees only its own product contract, outputs, and predecessor bundle ID/digest — not private source, fixtures, selectors, expected literals, vault paths, or tests-lane paths. Code review runs the predecessor sealed suite in scratch and must redact findings before they return to the builder.

On an untyped lane, a candidate collision raises typed `PRIVATE_PATH_COLLISION`. `REVIEWING_CODE` durably emits `TEST_INVALIDATION`, atomically resets the lane to `WRITING_TESTS`, passes the redacted actionable reason to the persistent tester, and preserves old artifacts as immutable history. Other `IsolationError` failures fail closed.

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

New installations stamp `dashboard` with `enabled`, absolute `launcher` from the installed skill (`apps/dashboard/bin/maestro-dashboard`), `api_port` 4600, `ui_port` 4317, and `open`.

The visualizer at `.claude/skills/sssf/apps/visualizer/` remains a read-only SQLite UI. The Next.js dashboard is detached observability. Neither is workflow authority.

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
