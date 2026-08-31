# Fable Goal: Build Maestro from the SSSF Fork

**Shipped execution (current):** this file is the 2026-08-13 architecture-session brief. Live Maestro is the nine-stage artifact factory in `MAESTRO_architecture.md`. Operator verbs are only `run start`, `run resume`, `run amend`, and `run status`. Durable authority is `lane_state.stage` plus immutable artifacts under an external `runtime_state_root`. Candidate, integration, and publication Git refs are immutable; publication is receipt-backed. Herdr and OMP are transport. Do not treat attempt identities, retry/skip/abandon/cancel/bootstrap/plan subcommands, synthetic review nodes, recovery markers, actor generations, or mutable candidate branches as live workflow authority. The Strav inventory below remains historical evidence.

You are the Fable team lead and principal architect for **Maestro**, the existing fork of `disler/super-simple-software-factory` (SSSF).

## Hard deadline

- Start `/team` immediately.
- Work until architecture is stable or **2026-08-13 08:55 AM America/New_York**, whichever comes first.
- Do not stop at the first plausible design. Run at least three cross-model review rounds.
- Reserve the final 20 minutes for convergence, contradiction removal, and writing the final artifact.
- At 08:55, stop investigation and emit the best decision-complete result. State any unproven predicate plainly; never hide it as “future work.”

## Goal

Starting from Maestro’s current SSSF fork, design the smallest robust software factory that can:

1. accept planning work from **planf3**, **arch-review/brownfield**, and **prd-to-strav** and produce one internally consistent, mechanically executable, finalized plan;
2. execute that plan as a two-lane dependency DAG: private test author → test review → seal → builder → code review; independent ready lanes may overlap; each accepted lane merges exactly once into one run integration ref; dependents start from that integration artifact; final review then receipt-backed publication to `main`;
3. give every lane exactly five persistent role agents (`tester`, `test-reviewer`, `builder`, `code-reviewer`, `integration-reviewer`) in one Herdr lane tab; keep each role's stable working tree and pane/session memory across `REVISE` and scheduler restart; reconnect only by proved project/run/lane/role identity and role-scoped cwd; refuse legacy stage/attempt panes, malformed or unreachable observations, mismatched placement, dirty boundaries, and unproved agents; resubmit the current stage from its immutable input and recreate only confirmed dead or typed `agent_not_found` roles;
4. launch agents in visible **Herdr** panes as transport only through an explicit launcher abstraction:
   - Claude directly;
   - OMP only with explicit `--profile`;
   - Kimi and Grok only through explicit configured routes;
5. retain SSSF’s typed envelopes, deterministic gates, bounded same-session correction, and SQLite trace for sequential ADWs; Maestro’s workflow authority is `lane_state.stage`, not envelopes, panes, idle status, or attempt rows.

The result must be an **expansion of SSSF**, not another Strav rewrite. Preserve SSSF’s understandable control plane. Add only the capabilities above and the minimum machinery needed to make them correct, durable, observable, and testable.

## Workspace and authority boundaries

### Implementation base

- Repository: `/Users/davidandrews/PycharmProjects/maestro`
- Origin: `git@github.com:GratefulDave/maestro.git`
- Starting commit: `de31374882e7a4e3e5b7bb9bd09e69dc2f779356`
- This is the existing SSSF fork. Do not create another repository.

### Read-only requirements/failure corpus

- Repository: `/Users/davidandrews/PycharmProjects/strav`
- Treat every Strav branch, source file, spec, test, run artifact, and historical design as **read-only evidence only**.
- Do not implement in Strav.
- Do not merge, cherry-pick, port, or revive any Strav branch.
- Do not assume a Strav abstraction deserves to survive. Extract requirements and failure lessons; redesign simply in Maestro.

### Reference implementation

Review SSSF at Maestro’s baseline:

- https://github.com/disler/super-simple-software-factory/tree/de31374882e7a4e3e5b7bb9bd09e69dc2f779356

Read at minimum in both upstream and Maestro:

- `README.md`
- `.claude/skills/sssf/SKILL.md`
- `.claude/skills/sssf/templates/adws/adw_modules/runner.py`
- `.claude/skills/sssf/templates/adws/adw_modules/agents.py`
- `.claude/skills/sssf/templates/adws/adw_modules/data_types.py`
- `.claude/skills/sssf/templates/adws/adw_modules/tracer.py`
- `.claude/skills/sssf/templates/adws/adw_modules/permissions.py`
- `.claude/skills/sssf/templates/adws/adw_modules/git_helper.py`
- `.claude/skills/sssf/templates/adws/adw_simple_sdlc.py`

GitHub is reference. Maestro is the only target.

## Mode: architecture only

Do not implement code in this session. Do not run agents that modify either codebase. Do not mutate plans, runs, receipts, secrets, worktrees, branches, installed packages, or user-owned `.omc` state.

The only repository file this session may create or replace is:

`/Users/davidandrews/PycharmProjects/maestro/MAESTRO architecture.md`

Produce one primary architecture and implementation plan for approval. No menu of alternatives.

# Mandatory `/team` protocol

Activate `/team` immediately. Use **Opus, Sonnet, and Fable** deliberately.

Minimum roles:

1. **Opus — cutover failure archaeologist**
   - reconstruct the complete Strav failure history from source, specs, tests, and Git history;
   - distinguish historical fixes from requirements still relevant to Maestro;
   - maintain a deduplicated failure-to-prevention matrix.

2. **Sonnet — SSSF/Maestro code archaeologist**
   - map actual control flow, state, prompts, typed envelopes, gates, retries, process execution, Git behavior, and SQLite tracing;
   - reject README claims when implementation differs.

3. **Fable — planning/finalization architect**
   - define how planf3, arch-review/brownfield, and prd-to-strav converge on one canonical executable plan;
   - eliminate parallel planning authorities and post-validation semantic surprises.

4. **Opus — DAG/worktree/integration architect**
   - define ready-set scheduling, parallel safety, nine-stage `lane_state.stage` authority, crash as stage restart from the last immutable artifact, ephemeral worktree ownership, immutable candidate/integration/publication refs, deterministic merge, conflicts, and receipt-backed publication.

5. **Sonnet — Herdr/launcher architect**
   - source-verify Herdr and launcher behavior;
   - define route validation, exact argv, pane/worktree binding, session/resume, output normalization, credentials, failures, and trace events for Claude, OMP, Kimi, and Grok. Herdr remains transport, not workflow authority.

6. **Fable — simplicity/adversarial reviewer**
   - attack every proposed component for duplicate authority, hidden state, unnecessary cryptography, retry-as-authority, nondeterminism, and operator burden;
   - continually compare design weight with SSSF’s simple spine.

The controlling Fable session owns decomposition and final synthesis only. It must not duplicate worker assignments. Workers must report to the lead and challenge one another directly. If `/team` cannot spawn Fable as a worker, the Fable lead performs Fable synthesis/review after Opus and Sonnet report. Do not silently omit any model family.

## Iteration protocol

### Round 1 — independent evidence

Each worker returns:

- observed facts with exact source paths/symbols or URLs;
- inferred requirements explicitly labelled `INFERENCE`;
- failure classes relevant to its scope;
- the smallest SSSF-compatible additions required;
- abstractions that must not be copied from Strav.

### Round 2 — cross-examination

- Opus reviews Fable’s planning/finalization design.
- Fable reviews Opus’s DAG/worktree design.
- Sonnet reviews both against actual SSSF code and simplicity.
- Herdr/launcher architect verifies every lane can launch and report through every required route; cancel is not a Maestro operator verb.
- Failure archaeologist checks every known Strav failure against a target prevention or detection.

Every challenge names a violated invariant, unsupported assumption, duplicate authority, missing transition, or unverifiable acceptance claim.

### Round 3 — tabletop and mutation review

Walk the complete golden scenario and all required negatives below. Mutate each load-bearing edge and identify the deterministic failure.

### Stability rule

Architecture is stable only after **two consecutive cross-model passes** produce:

- no new P0/P1 contradiction;
- no unresolved authority ownership;
- no unspecified state transition;
- no launcher without exact configured behavior;
- no Herdr pane/worktree ambiguity;
- no known Strav failure without prevention/detection mapping;
- no acceptance criterion based only on prose, existence, agent self-report, or a mock disconnected from the real seam.

If stability is not reached by the deadline, choose the simplest safe design and state exactly what remains unproven. Never fabricate consensus.

# Evidence baseline

## What SSSF demonstrably gets right

Preserve these unless evidence proves conflict with the goal:

1. deterministic code owns ordering, retries, acceptance, and known commands;
2. agents own bounded judgment/work only;
3. one small execution primitive (`run.phase(PhaseParams)`) forms the understandable spine;
4. Pydantic envelopes, not transcripts, cross seams;
5. gates validate claims after output;
6. malformed output and failed gates receive bounded same-session correction;
7. tool capability and write authority are separate;
8. phase completion and overall acceptance are distinct;
9. events stream into SQLite while work occurs;
10. workflow scripts remain thin Python over reusable modules.

Verified baseline limitations:

- SSSF is a sequential source-order phase list, not a dependency DAG.
- `events.parent_id` is trace nesting, not a dependency edge.
- Pi is the only implemented launcher; Claude Code is a stub.
- provider/model strings are not launcher contracts.
- SSSF runs on the current branch.
- no worktree-per-node, sandbox, merge, deterministic integration, or approval/finalization exists.
- fresh quality commands are placeholders that exit zero.
- provider credentials may fail only after a chain starts.
- `commit_all()` stages the whole current working tree.
- SQLite WAL enables live reads; it does not implement scheduling.

Do not mistake “Python owns the graph” in README for an implemented dependency scheduler.

## Complete Strav failure corpus to verify and map

This inventory is context, not a solution. Expand it from `git log --all`, specs, tests, run audits, and source. Read at minimum:

- `strav-native-plan-authority-cutover-prompt.md`
- `StravV3_cutover.md`
- `specs/strav-48-hour-stabilization-pr-review.md`
- `specs/design/hardening-gates-1-5.md`
- `specs/design/hardening-gates-6-10.md`
- `specs/plans/brownfield-skill-repairs.md`
- `docs/run-v3-fb5c-defects.md`
- `docs/cutover-authority-ledger.json`
- `docs/cutover-authority-inventory.json`
- `assets/planf3/SKILL.md`
- `assets/arch-review/SKILL.md`
- `assets/plan-brownfield/SKILL.md`
- `assets/prd-to-strav/SKILL.md`
- `assets/runplan-finalize/SKILL.md`
- `strav/protocol/plan_package.py`
- `strav/protocol/eligibility.py`
- `strav/workflows/review.py`
- `strav/cli.py`
- `strav/protocol/envelope.py`
- `strav/machine/reducer.py`
- `strav/machine/table.py`
- `strav/machine/driver.py`
- `strav/runners/claude_raw.py`
- `strav/runners/omp.py`

### Planning and authoring failures

- Strav never owned one reliable end-to-end plan authoring loop; external skills produced structures interpreted differently downstream.
- Plan IR, HTML, receipts, WorkflowSpec, PlanPackage, prepared authority, ledgers, review inputs, envelope, and projections became overlapping/translated authorities.
- Translation dropped, renamed, inferred, duplicated, or reinterpreted semantics.
- Schema contracts drifted across skills, fixtures, docs, vendored assets, installed snapshots, and live artifacts.
- Current immutable evidence and future producer-created implementation/test outputs were not honestly distinct.
- Future tests masqueraded as current evidence, causing invented paths, unrelated substitutes, narrowed behavior, or implementation-before-plan.
- Ambient cwd made commands look executable in the wrong repository/worktree.
- Self-referential, vacuous, aggregate-only, existence-only, mock-only, skipped, or `|| true` verification passed without proving behavior.
- Test selectors were invented or pointed at unrelated tests.
- execution-floor/`min_executed` semantics were undefined, mismatched, ignored, zero, or unparseable.
- baseline-red future behavior was confused with passing current evidence.
- monolithic plans exceeded reviewer context and multiplied token cost.
- “valid with caveats” moved known blockers into expensive finalization.

### Validation, evidence, and review failures

- structural validation, semantic preflight, independent control, and finalization review enforced different contracts.
- generic/non-normative rubric text made reviewers invent applicability/severity.
- the same plan semantics received zero findings from one reviewer and many from another.
- reviewers rejected topology the package schema required or permitted.
- deterministic validators missed graph-checkable blockers; semantic review became first detector.
- attempted fixes also promoted optional/context-dependent fields into false universal blockers.
- claims, guards, mutations, sources, and evidence were syntactically present but not exercised.
- wrong-tier or same-vendor review, verdict-less completion, and late verdict loss occurred.
- preflight/control/final review duplicated expensive analysis.
- reviewer findings existed only in transcripts and could not be audited later.
- exact-byte binding proved identity, not calibrated semantic judgment.
- ledgers, rendered views, receipts, sidecars, and source pins could be stale, mutable, forgeable, unbound, or incompletely inventoried.

### Finalization, identity, and secret failures

- identity/cache inputs were incomplete in some generations and over-bound nonsemantic inputs in others.
- HMAC `reviewer_key_id` entered semantic package bytes; losing the raw key made a reviewed package impossible to finalize.
- a SHA-256 digest could not recover the secret.
- rebinding changed the package digest and invalidated reviews although plan semantics were unchanged.
- no supported key rotation/rebind existed.
- secret setup/export/storage instructions were missing, unsafe, or inconsistent.
- package, authority, planning route, ledger, receipt, key, route, and digest mismatches surfaced serially and late.
- create-once authority names/revision identities confused operators.
- terminal FAIL required new identities because authoring failed to find blockers earlier.
- dormant repair/re-review authority coexisted with single-pass finalization.
- CLI/docs drifted (`finalize`/`finalize-package`, `run`/`run start`).
- installed bytes differed from source while cwd imports falsely tested source.

### Runtime and state failures

- lane state, verdict sidecars, markers, drafts, caches, journal, pane status, and projections competed for truth.
- pane output/status was treated as completion/liveness authority.
- stale checkpoints/gates blocked progress.
- orphan reclaim/pane adoption attached wrong process or worktree.
- late results were judged against mutable current state rather than causing attempt.
- source events were silently rejected/lost.
- event schema evolution broke historical digest/replay.
- command state could remain stuck after non-progress events.
- invalid defaults manufactured commands missing required SHAs.
- unsatisfiable effects retried forever; caps covered only one failure family.
- fixer budgets/context grew without bound or reset at wrong boundaries.
- resume choreography added special cases instead of one durable replay model.
- dashboard, fixtures, demos, and watchers consumed stale/invented schemas.

### DAG, integration, Git, and provenance failures

- dependency graphs existed, but runtime action execution remained serial.
- “parallel lanes” did not mean concurrent ready-node execution.
- agents/integrators claimed merges without Git ancestry.
- test PASS was confused with merge provenance.
- integrators stopped early or accepted content-blind/zero-row checks.
- worktree/base SHA/attempt/output commit/merge head were inconsistently bound.
- conflicts, replacement pins, merge ordering, and recovery were incomplete.
- environmental failures were blamed on code lanes and triggered semantic repair.
- dirty stabilization branches conflicted with clean-cutover work.
- narrow fixes passed focused suites but failed under live resume, retry, late-result, and interleaving.

### Herdr, launcher, and routing failures

- OMP blocked on an unseen prompt and looked frozen.
- Claude kickoff text/Enter was dropped during TUI transition.
- `/team` injection was best-effort rather than capability-checked.
- panes were stale, busy, closed, or mis-adopted.
- pane cwd diverged from lane worktree.
- Herdr CLI drift was swallowed as “no pane.”
- producer vendor/route facts were lost on resume.
- route, launcher, model, profile, and vendor independence were conflated.
- current Strav has first-class Claude direct and OMP only; generic Herdr route flexibility is not verified Kimi/Grok support.
- pane text must never become lifecycle truth.

### Retry, cost, operations, and proof failures

- re-authoring repeatedly minted immutable identities rather than correcting all blockers before finalization.
- huge plan payloads forced several reviewers to ingest identical bytes.
- duplicate delegation/controller impatience repeated assigned work.
- cold restarts discarded useful context.
- agents/panes ended without durable reports, secrets, or findings.
- green suites substituted for live journal/state progress.
- anti-inert controls were missing; tests stayed green after wiring removal.
- `uv tool install` snapshots were treated as editable.
- source, vendored assets, trusted pins, generated HTML, installed bytes, fixtures, docs, and CI evidence drifted.
- manual generated docs and stale command examples contradicted runtime.
- EPA/live runs became the test harness for generic architecture defects.
- recovery advice proposed nonexistent commands, one-off edits, or another expensive review.
- no permanent golden author → finalize → DAG run → merge → restart/replay scenario existed.

## Strav requirements candidates, not code to reuse

Fable must verify each and either preserve it or explicitly reject it:

- one canonical executable plan is semantic authority;
- deterministic checks precede semantic review;
- exact plan identity publishes at most one runnable identity;
- finalization does not itself launch work;
- one independent semantic decision has durable PASS/FAIL semantics;
- typed events/envelopes, not pane text, advance lifecycle;
- merge success requires Git ancestry, not only tests;
- all tests use fake launchers/Herdr seams, never real agents;
- Claude launches directly;
- OMP receives exactly configured `--profile`;
- dependency readiness and merge order are deterministic;
- current and future evidence are different types.

# Required target behavior

## A. Proper planning and finalization

Define exact contracts for:

- `planf3`;
- `arch-review/brownfield`;
- `prd-to-strav`.

They may differ in discovery/analysis but must converge before execution on **one canonical executable plan model**. Narratives, HTML, receipts, reports, and transcripts may be evidence; none may be parallel runtime authority.

The minimum model must make these distinctions explicit and enforceable:

- observed current source/evidence;
- expected producer output absent at base commit;
- semantic hypothesis requiring review;
- code lane versus agent lane;
- lane input/output ownership;
- dependencies (`needs`) and declared outputs;
- immutable candidate/integration/publication refs, not mutable branches;
- launcher route (transport only);
- gates and exact acceptance behavior;
- integration/merge responsibility (exactly-once merge into the run integration ref).

Do not import Strav’s frozen model wholesale.

Authoring emits exactly one of:

### `FINALIZATION_ELIGIBLE`

- one canonical plan digest;
- all deterministic obligations resolved;
- no known material caveat;
- DAG acyclic/executable;
- inputs, outputs, gates, and merge policy complete;
- safe for exactly one independent semantic decision.

### `AUTHORING_BLOCKED`

- no reviewer launched;
- exact typed blockers and pointers;
- no package/run publication;
- no vague prose epilogue.

Finalization must be simple:

- review exact canonical plan under one explicit normative rubric;
- one independent semantic decision, not preflight/control/final duplication;
- PASS publishes one immutable runnable identity;
- FAIL publishes no runnable identity and is terminal for those plan bytes;
- replay causes no second review or duplicate run;
- if authentication is retained, semantic identity cannot depend on an unrecoverable secret;
- key rotation cannot force semantic re-review of unchanged plan bytes;
- scheduler accepts only finalized identity.

## B. DAG readiness and true parallelism

Add the smallest explicit DAG extension to SSSF. Specify:

- lane/edge model (exactly two dependent lanes in the proven slice);
- cycle and reference validation;
- ready-set: stage is not `MERGED` or `WAITING_FOR_USER` and every `needs` lane is `MERGED`;
- independent ready lanes may author/review/build concurrently; merges are serialized;
- nine-stage `lane_state.stage` as the sole durable workflow authority;
- crash resume restarts the incomplete stage from the last accepted immutable artifact;
- `run amend` invalidates only the affected stage and transitive dependents;
- reviewer `PASS`/`REVISE` as artifact data, never synthetic review nodes;
- dependency integration artifact accepted before a dependent builder starts;
- SQLite representation of stage and artifacts only — no attempt identity, recovery marker, or actor generation as authority.

Independent ready lanes must overlap in execution. A plan DAG serviced by one serial action loop fails this requirement.

Choose one durable lifecycle authority: `lane_state.stage`. SQLite stores it; JSONL, envelopes, process/pane state, worktree files, and UI cannot compete. Define transaction, crash, and replay as stage-boundary commits.

## C. Per-node worktrees and deterministic merge

Specify:

- clean immutable stage input;
- exactly one stable role-scoped checkout or private tree per persistent lane role; never treat a dirty tree or unproved pane/process as workflow authority;
- private tester and test-reviewer trees never enter the product repository or its Git object database;
- role pane cwd remains in its stable role scope across `REVISE` and scheduler restart;
- allowed writes;
- artifact digest and commit SHA binding;
- dirty/unauthorized-write handling: refuse boundary violations; resume from the last immutable artifact without replacing a proved role session;
- cleanup;
- deterministic merge-ready ordering despite nondeterministic finish order;
- ancestry proof on the integration ref;
- merge conflict refuses without moving the ref;
- post-merge acceptance;
- downstream invalidation via `run amend`;
- no `MERGED` stage without Git proof on `refs/maestro/integration/<run-id>`.
Never merge a mutable candidate branch. Candidates are immutable refs/SHAs on `BUILDER_OUTPUT`.

## D. Herdr and launcher abstraction

All agent nodes launch in visible Herdr panes. Herdr is transport/observability, never lifecycle authority.

Define one adapter contract covering:

- one workspace per project+run, one tab per lane, exactly five sibling panes named `tester`, `test-reviewer`, `builder`, `code-reviewer`, and `integration-reviewer`;
- reconnect a proved live stable role agent and resubmit its current stage; never adopt or rename legacy stage/attempt, stale, closed, busy, malformed, unreachable, mismatched, or unproved panes as current roles;
- bind every role pane cwd to its stable role-scoped checkout or private tree;
- launch/readiness/stream/exit (transport cancel is not a Maestro operator verb);
- preserve all five role sessions for the run; reviewers return actionable redacted feedback to the existing implementation agent; idle after output is not completion;
- typed normalized events as evidence only;
- no durable pane/agent/attempt identity as workflow authority (`actor_sessions`/generations remain forbidden);
- token/cost accounting as observation;
- fake Herdr adapter for offline tests.

Required routes:

### Claude

- direct Claude executable through Herdr;
- explicit model and effort/thinking;
- exact argv and PTY contract;
- session resume/correction;
- never route Claude through OMP.

### OMP

- exact `--profile <profile>` from config;
- no default/fallback profile;
- exact argv/session/readiness/failure behavior.

### Kimi and Grok

- explicit configured routes;
- no inference from vendor/model strings;
- source-verified binary/API adapter and argv/config;
- if not available locally, fail capability preflight before DAG launch rather than invent a command.

Every route defines binary/provider, model/profile/effort, env/credentials, capability preflight, argv, process/session behavior, normalized outputs/errors, and fake coverage. Launcher behavior cannot hide in prompts.

## E. Typed envelopes, gates, SQLite trace

Preserve SSSF sequential-ADW boundaries; Maestro does not use envelopes as stage:

- typed envelope per sequential ADW phase only;
- one canonical schema source; generate/check prompt Report examples against it;
- deterministic post-output gates;
- gates prove behavior/outputs, not existence only;
- known commands are code phases;
- bounded same-session correction is sequential-ADW only, not a Maestro retry budget;
- raw output and pane text are evidence only;
- Maestro SQLite records plan digest, run, `lane_state.stage`, immutable lane/run artifacts, integration/publication receipts; Herdr panes/sessions are observation;
- one query path serves live and history;
- no dashboard-only schema or fixture-only truth.

# Simplicity budget

The target must remain readable by one engineer. Final design must report:

- module/file tree;
- approximate production LOC per module;
- count of canonical models;
- count of durable authorities;
- count of state machines;
- semantic reviewer calls per identity;
- writes/transactions per node transition;
- concepts deliberately not copied from Strav.

Reject any component that does not prevent a named failure, satisfy a requested capability, or remove more complexity than it adds.

Prohibited unless necessity is proven:

- parallel plan authorities;
- Plan IR → WorkflowSpec → envelope semantic translation chains;
- mutable sidecar lifecycle truth;
- reviewer-of-reviewer loops;
- repair actors after immutable finalization;
- HMAC secret in semantic plan identity;
- generated HTML as authority;
- hidden route inference;
- pane text/status as completion;
- separate authoring/finalization/runtime validators;
- compatibility shims for Strav;
- EPA/run-specific IDs, SHAs, names, or exceptions.

# Golden offline scenario

The architecture and implementation plan must prove this using fake Herdr/launcher adapters and a fixture Git repo:

1. PRD plus brownfield repo enter appropriate planning workflows.
2. Planning yields one architecture decision and three implementation packages.
3. Two producers are independent and ready together; a third depends on both.
4. One producer creates implementation and a test absent at base.
5. Plan types that future test as producer output, never current evidence.
6. deterministic authoring emits `FINALIZATION_ELIGIBLE` with one digest;
7. identical inputs reproduce identical canonical bytes/digest;
8. one fake independent PASS publishes exactly one runnable identity;
9. replay performs no second review/publication;
10. scheduler starts the two independent ready lanes concurrently in separate ephemeral worktrees and visible fake Herdr panes;
11. pane cwd matches each worktree;
12. at least two launcher kinds execute; full matrix covers Claude, OMP, Kimi, Grok;
13. dependent cannot start before every `needs` lane is `MERGED`;
14. each success binds immutable artifacts, `lane_state.stage`, worktree input commit, and Git SHA — not an attempt identity;
15. gates reject missing/wrong output, zero/skipped tests, unrelated tests, and unauthorized writes;
16. accepted lanes merge exactly once into the run integration ref with ancestry proof;
17. merge conflict refuses without moving the ref;
18. post-merge acceptance proves nonzero real checks;
19. after process death, resume reconstructs the incomplete stage from the last immutable artifact, not from pane or dirty-tree state;
20. publication is receipt-backed after passing final review; `main` at the same SHA without a receipt is refused.

# Required negatives and anti-inert cases

At minimum:

- DAG cycle, missing dependency, duplicate output owner;
- future output declared current, or current fact disguised as future output;
- nonexistent/unrelated selector; zero/skipped/unparseable execution;
- concurrent lanes share a worktree;
- pane cwd mismatch; stale/closed/busy/mis-adopted pane treated as stage;
- pane text falsely says “done” without an accepted artifact/stage commit;
- stale prior-stage result treated as current input;
- missing launcher binary/profile/credential;
- OMP without `pm_profile`;
- inferred Kimi/Grok route;
- Claude routed through OMP;
- unauthorized write; dirty/divergent worktree adopted; missing output commit;
- claimed merge without ancestry; nondeterministic ordering; merge conflict that still moves the ref;
- treating launcher failure as a Maestro retry/skip/abandon verb;
- semantic finalization FAIL;
- same-identity replay; changed bytes with stale receipt;
- key rotation with unchanged plan;
- crash during a stage; crash between completion and the durable stage commit;
- remove scheduler edge, gate invocation, publication wiring, ancestry guard, profile propagation, pane/worktree binding, or one SQLite stage/artifact write.

For every mutation name the deterministic diagnostic/state/test that goes red and one unrelated control that remains green.

# Required final artifact

Write `/Users/davidandrews/PycharmProjects/maestro/MAESTRO architecture.md` with exactly these sections:

1. **Acceptance predicate** — machine-checkable success and failure, no proxies.
2. **SSSF/Maestro baseline map** — exact files/symbols, preserved spine, extensions, limitations.
3. **Complete Strav failure inventory** — evidence, historical/current status, prevention/detection per row.
4. **Root causes** — smallest cause set behind the four-week cycle.
5. **Target Maestro architecture** — components, ownership, one data-flow diagram, one durable authority.
6. **Canonical planning/finalization model** — concrete models/pseudocode, three planning modes, evidence types, eligibility, review, replay.
7. **DAG scheduler/state machine** — nine `lane_state.stage` values, frozen transitions, ready-set, concurrency, stage-restart resume, `run amend`; no retry/cancel/recovery-marker authority.
8. **Worktree/deterministic merge protocol** — Git invariants, immutable candidate/integration/publication refs, order, conflicts, ancestry, cleanup.
9. **Herdr/launcher architecture** — pane binding plus exact Claude/OMP/Kimi/Grok route/process/session/error contracts; transport only.
10. **Typed envelopes, gates, and SQLite schema** — sequential-ADW envelopes versus Maestro stage/artifacts; ownership, transactions, authority versus observation, query path.
11. **Public operator workflow** — frozen CLI `run start|resume|amend|status` only; no plan/bootstrap/retry/skip/abandon/cancel subcommands.
12. **Greenfield implementation plan in Maestro** — ordered slices from current fork, exact files/interfaces, no Strav reuse.
13. **Verification matrix** — golden, negatives, anti-inert controls, fake Herdr/launchers, install/smoke and before→after proof.
14. **Simplicity accounting** — modules/LOC, authority/state/reviewer/write counts, rejected complexity.
15. **Migration and first real plan** — replacement gate and first EPA/real-plan action; no live Strav-state migration.
16. **Decision log and dissent** — tested decisions, rejected alternatives, unresolved proof at deadline.
17. **Flat implementation checklist** — exhaustive ordered observable completion; no MVP/later/placeholders/shims.

# Final quality gate

Every team member must answer before finalization:

1. Does this remain recognizably SSSF rather than rebuilding Strav?
2. Is there exactly one canonical executable plan?
3. Is every known Strav failure prevented, detected, or explicitly ruled out?
4. Do independent ready lanes actually overlap?
5. Can each lane prove `lane_state.stage`, immutable artifacts, worktree input commit, output SHA, gates, and merge ancestry — without an attempt identity?
6. Are Claude, OMP, Kimi, and Grok explicit preflighted routes?
7. Can a lost secret invalidate unchanged plan semantics?
8. Can any sidecar, prompt, pane, transcript, UI, or prose advance lifecycle state?
9. Can replay duplicate review, publication, work, or merge?
10. Does every reported green have a planned anti-inert red?
11. Can one engineer maintain this in six months?
12. Is every component justified by a named failure or requested capability?

Any “no” blocks stability. Iterate until corrected or the deadline arrives.

Start `/team` now.