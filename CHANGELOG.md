# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [Unreleased]

### Changed

- `docs/plan-authoring.md`: the single-repository step-by-step is rewritten around three
  intended plan-CLI verbs — `maestro plan gate`, `plan review`, `plan ship` — each taking only
  the plan name, everything else (including the `.maestro/` artifact layout and `--repo-root .`)
  resolved underneath. The Plan IR, its bound HTML view, and its PASS review receipt live under
  `.maestro/` (`.maestro/<name>.plan.json`, `.maestro/<name>.html`,
  `.maestro/<name>.plan-review.json`) instead of at the repository root, alongside
  `.maestro/plans/<name>/maestro-plan.v1` where Maestro projects the finished plan; the
  `planctl render`/`validate`/`mutate`/`review` calls each verb wraps carry `--repo-root .` so
  `source_artifacts` paths resolve from the repository root instead of `..`-relative to the IR's
  own directory inside `.maestro/`. Those `planctl`/`maestro plan` calls are now presented as a
  clearly marked "What each verb runs" subsection, not as the reader's typed instructions.
  Explains why `gate` and `review` are separate commands: the reviewer's HMAC key is minted and
  held by Maestro itself (`maestro bootstrap`, alongside the existing Ed25519 signing material)
  and injected only into `plan review`'s subprocess, never into the author's shell; `gate`
  refuses outright if that key is present in its own environment. Notes plainly that these verbs
  are not yet built — they depend on an in-progress, unmerged companion change in lexgenius.

### Added

- `docs/plan-authoring.md`: the path from a source document to an executable
  plan. Records that source documents are never converted — they are pinned as
  `source_artifacts` inside a `plan-contract.v1` Plan IR — and that the IR must
  carry `extensions.maestro` before review, because the review receipt is bound
  to the IR bytes. Covers the single-repository sequence and the
  finalize-then-compose rule for `maestro-workspace.v1`.

- `maestro bootstrap`: visible Herdr route admission. Captures first turn,
  continuation, pane cwd, and clean cancellation per configured route; mints
  or reuses Ed25519 key material under the repository state root; writes a
  detached-signature `maestro-route-receipt.v1` only after that capture
  verifies. Existing unsigned or junk files are refused, not overwritten.
- `maestro plan author`: the production writer of canonical `maestro-plan.v1`
  bytes. Reads a conventional draft, fills git-observed hashes, and
  create-once writes `canonicalize(plan)`.
- Named-plan commands fall back to bootstrapped `keys/signing.pub`,
  `keys/signing.seed`, and `keys/route.pub` when the configured environment
  variables are unset.

- `tests/test_no_dead_seams.py`: a structural guard that every production reader has a
  production writer. `MAESTRO_architecture.md` Family B item B15 makes a check field with
  zero *readers* a build failure; this enforces the mirror, which is the direction that had
  been reaching production — a typed field, dataclass attribute, enum member, or callable
  that production reads or branches on but never writes or calls. Such a value is
  structurally unreachable, the branch guarding it silently never runs, and the suite stays
  green because the tests construct the value by hand. An AST reference analyser over all 124
  `.py` files in the runtime found eleven further instances of that class beyond the ones
  fixed here. Deliberately test-only symbols remain legal but no longer silent: each of the
  50 allowlist entries carries the reason it has no production writer, so landing a fix means
  deleting a line. Attribution is by the class the callee resolves to, not by the bare name,
  which is what lets it see through a same-named keyword argument on an unrelated class —
  the masking that hid `SchedulerDeps.min_cases` from every grep anyone would have written.
  The check for stale allowlist entries is present but disabled for now. Its own docstring
  states what it cannot catch: dynamic writers, writers sitting inside unreachable branches,
  and semantic deadness.
- `tests/test_launcher_classification.py`: drives the launcher-failure path through the
  production adapter rather than a hand-built `FailureSignal`, and asserts structurally that
  no raw `.launch(` call site remains in `maestro.py`.
- `tests/test_min_cases_enforcement.py`: asserts that a plan's declared threshold survives
  projection onto `PlanNode` and is the value each of the three adjudication sites counts
  against.

### Fixed

- The reviewer is given the node's contract instead of a placeholder. `PlanNode` carried no
  `instruction` field and `Plan.to_plan_nodes` projected the node field by field, so the
  instruction never crossed the projection. `code_review.py` read it as
  `getattr(node, "instruction", "")`, always received `""`, and fell through to
  `_fallback_instruction` — which told the reviewer of every agent node, in every run that
  has ever run, to "make the gate pass". The node's goal, its `produces`, its acceptance,
  and its `min_cases` never reached the reviewer at all, so the declared reviewer contract of
  `MAESTRO_architecture.md` §3.6 B9 was degenerate in production while the whole suite stayed
  green. `min_cases` had been dropped by the same projection earlier and fixed as a one-field
  patch, which is why `instruction` was still there to find. The projection now carries the
  field, and a class-level projection-totality guard compares every declared field between the
  two representations by name **and** by value, so the next dropped field fails the build
  rather than the review. Recorded as §19 M1 with the lesson it binds (§17 item 115).

- The write-permission check measures the delta over git's tree, not over the disk.
  `worktree.py::inventory()` enumerated the attempt worktree with `os.walk` under a comment
  stating "No excludes and no ignore list", so §8.3's permission check counted **16,090
  gitignored `.venv/**` paths** as writes outside the node's declared globs and discarded an
  attempt that had run **209 turns** of real work. Every path in the conviction was real,
  which is what let it survive. The inventory is now bounded to
  `git ls-files --cached --others --exclude-standard`; the unbounded `offending_paths` join
  that made the resulting message unreadable is bounded at the point of rendering; and a
  B13-shaped prompt size preflight was added on the same path. Recorded as §19 M2
  (§17 item 116).

- Post-split launch failures raise a typed refusal. Every launcher exit after the process
  split raised an untyped `HerdrCallError`, so `LaunchFailed.pane_created` had nothing to read
  and fell back to its fail-closed default of `True`; the scheduler then demanded quiescence
  for a pane handle that had never been registered, and `PROCESS_GROUP_UNTRACKED` converted a
  retryable launch failure into terminal `QUIESCENCE_UNPROVEN`. The cleanup had in fact
  succeeded — the run died terminally because of it. Every post-split exit now raises
  `LaunchRefused`, constructed after cleanup has run. Recorded as §19 M3 (§17 item 117).

- `AGENT_PROMPT_UNOBSERVED` is distinct from `AGENT_PROMPT_UNSUBMITTED`. One terminal code
  covered both "the pane's revision counter could not be read" and "the pane's revision
  counter did not move", so a momentary failure to read the instrument retired an attempt that
  would have succeeded on retry. The first is transient and a property of the observer; the
  second is terminal and a property of the agent. Recorded as §19 M4 (§17 item 118). Landed
  alongside 23 stale tests repaired (herdr fakes that carried no `revision`, the argv-borne
  prompt, the ship fixtures), 8 unmirrored parity files, and a fix for `plan ship` mutating
  disk above `pane.open()`, which made the verb non-resumable without hand-deleting the plan
  it had just written.

- A run reaches a terminal state when its scheduler exits. `latest_outcome` had exactly one
  writer, `Scheduler._declare`; a scheduler that was killed, crashed, or cancelled through
  `_run_cancel` — which never touched the column — left its run reading live forever, and
  `run list` reported two runs dead at 0 turns as still running. Run state is now derived from
  node states plus scheduler liveness, with a new `ABANDONED` state for a run whose scheduler
  is gone and whose nodes are not terminal. In the same change the B13 prompt size preflight
  moved to the universal chokepoint in `HerdrLauncher.launch`, above the process split, so its
  coverage is a property of the call graph rather than of a route list. Recorded as §19 M5 and
  M6 (§17 items 119 and 120).

- The visualizer stops serving a deleted run as live. It opened the ledger with
  `immutable=1`, so SQLite never re-read the file and deleted runs kept rendering as live
  panes, and `MaestroRunDetail.vue` never cleared `run` on a 404, so a detail view outlived
  its subject. A diagnostic surface that fails in the same direction as the defect it exists
  to reveal masks that defect; here it would have masked the run-state defect above. Recorded
  as §19 M7.

- An agent's transcript path is waited for, not read once from the start payload. `herdr
  agent start` returns as soon as the server holds the process, which on a route that writes
  a JSONL transcript is before the coding agent has created the file; Herdr then omits
  `agent_session` from that payload entirely. `HerdrLauncher.launch` read the path only from
  the start payload, so whether an attempt got a transcript was a race against the coder's
  first write. The attempt that lost it had no path to register with the `TranscriptTailer`,
  was refused `SESSION_PATH_MISSING`, and died with `launched_at` NULL and `turn_count` 0.
  Measured on 2026-08-17: of three attempts on one node, the one that won the race ran 61
  turns and the two that lost it died at turn zero. `launcher.wait_for_agent_transcript()`
  now polls `herdr agent get` up to `TRANSCRIPT_PATH_TIMEOUT_S` (60.0s) whenever the start
  payload yields nothing, and the tailer is registered with the resolved path. Whether the
  path has arrived is read from the typed `agent_session.kind` field and never from pane
  text, so no lifecycle transition keys on prose (§1.2). The bound is 60s rather than the
  180s readiness gate because the agent has already been waited to idle at its composer by
  this point: a session file absent after a minute is absent, not late, and the caller's
  `SESSION_PATH_MISSING` refusal is then correct.

- Every `LaunchSpec` in `maestro.py` carries the redirected scratch environment. `LaunchSpec`
  declares `environment` with `field(default_factory=dict)`, so a construction site that
  omits it produces `{}` rather than a type error, and `pane_env_flags` then refuses the
  launch with `LAUNCH_REFUSED:SCRATCH_REDIRECT_MISSING` naming all seven `SCRATCH_ENV_KEYS`.
  The omission existed at all four construction sites — the node build launch, the node
  reviewer, `_reviewer_window_factory` (plan finalize), and `_deliver_author_turn` — so
  `maestro deliver` could never have launched an author at all, and the code-review path
  discarded 61 turns of completed work at the moment it tried to open a reviewer pane. A
  required argument given a permissive default is invisible at every call site that forgets
  it; the byproduct-redirection fix below made the refusal correct, and this makes the
  callers correct.

- The typed launcher-failure chain has a production writer, so `LAUNCHER_TRANSIENT` is
  reachable from the real adapter. `MAESTRO_architecture.md` §16.3 item 42 recorded the
  defect: `NodeExecution.launcher_failure` was read by `Scheduler._settle_verdict` and
  branched on by `retry_policy.classify`, and nothing on any production path ever assigned
  it, so every launcher fault fell through to the ENVIRONMENTAL default and §7.5's
  zero-retry rule for `LauncherFailure.CREDENTIAL` could not fire. Four pieces close it.
  `scheduler.LaunchFailed` is a typed exception carrying an `rp.LauncherFailure`;
  `maestro._launcher_failure_for` is the first production caller of the adapter contract's
  `LauncherAdapter.classify`, mapping `launcher.ErrorClass` onto that enum; `_typed_launch`
  and `_typed_launch_pane` wrap every launch and poll in the module, so no raw `.launch(`
  call site remains; and the scheduler's containment handler reads `exc.failure` into the
  `FailureSignal`. `_settle_failure` then sizes the budget with `rp.launcher_retry_budget`
  rather than `config.retry_budget`, because the budget is a property of the member and not
  of the class, and `_budget_reason` gained a CREDENTIAL arm so a zero-budget refusal blocks
  as `CREDENTIAL_REFUSED` instead of claiming a budget ran out that never existed.
  Classification still keys on the exception's type and a typed enum member, never on the
  message: `NodeExecution.launch_detail` carries the launcher's own prose to
  `transitions.detail_json` for the operator, and no guard reads it back. `launcher.py`'s
  typed vocabulary needed no change — it already existed and was simply never consulted.

- A plan's declared `min_cases` reaches the adjudicator. `Plan.to_plan_nodes()` projected
  each gate's runner, argv, and selector onto `scheduler_types.PlanNode` and silently dropped
  its `min_cases`, because `PlanNode` had no field to hold it. All three adjudication sites
  — the pre-gate, the post-gate, and final acceptance — instead read `SchedulerDeps.min_cases`,
  a single per-run scalar that no production caller ever set, so every gate in every run was
  counted against its default of 1 while plans declared 5, 6, 8, and 70. A plan told its
  agent 70 and verified it at 1; `run-4ee9e079f9124b8cbe4c416923e34170` reached ACCEPTED that
  way. One scalar per run could not have been right in any case: a run has many node gates,
  each with its own threshold. `PlanNode.gate_min_cases` now carries the per-gate value and
  is validated (`>= 1` on an agent node, exactly the default on a code node, which has no
  gate to count for); `SchedulerDeps.integration_min_cases` replaces the deleted scalar and
  serves the one integration gate per run that it was accidentally right about.

- An agent's declared result outranks the pane's reported status. `poll()` returned
  `PollState.GONE` on `agent_not_found`, and on a non-dict agent payload, before reading the
  envelope, so an agent that finished its turn, wrote `{"success": true}`, and exited was
  scored as an environmental failure. Herdr drops the agent record as soon as the session
  ends, so the faster the agent, the more reliably it lost the race. Observed in run
  `run-14b7b75944094c52ac9c0add41ae46a2`: three attempts, three complete success envelopes
  of 330, 271, and 326 bytes, all three discarded, the node blocked
  `ENVIRONMENTAL_BUDGET_EXHAUSTED` with no lane merged. `poll()` now consults a new
  `_declared_result()` first — the envelope file, then the transcript's terminal record —
  and GONE is reachable only when the agent is absent *and* nothing was declared. The rule
  was already stated in the file's own comments, that what the agent wrote beats what the
  pane reports; the code asked the pane first regardless. §9.7.

- A quiescence proof asks whether the process is gone, never whether the work succeeded.
  `cancel()` established quiescence by calling `poll()` and requiring GONE, so giving the
  envelope precedence inside `poll()` would have raised `HERDR_QUIESCENCE_UNPROVEN` on every
  successful node: `cancel()` runs in a `finally` after every attempt, and a successful
  attempt is precisely the case that now reports EXITED. The two questions are now asked by
  two predicates — `poll()` reports the attempt's outcome, `_agent_absent()` reports only
  whether Herdr still holds a record of the agent. The existing cancel tests could not have
  caught the regression, because none of them writes an envelope. §8.3, §9.7.

- Watchdog-driven environmental failures record which signal convicted the attempt.
  `Watchdog._stall` decides between `NODE_TIMEOUT`, `PROCESS_DEAD`, and `TURN_TIMEOUT` and
  passes the value to the scheduler's `fail` callback, which accepted the argument and never
  used it. `_failure_detail` therefore received neither a classifier reason nor a
  verification verdict and wrote `{}`, so every watchdog-driven ENVIRONMENTAL retry row and
  every `ENVIRONMENTAL_BUDGET_EXHAUSTED` block named the class and nothing else. The typed
  value existed at the point of failure and was dropped one hop before the ledger. The two
  neighbouring arms that also reach `_settle_failure` without a verdict are fixed in the same
  way: the worker's containment handler records the exception type and message, in the shape
  the quiescence arm already used, and the `check_at_create` arm records the failing check's
  own `CheckResult.detail`. `watchdog.py` needed no change; it was already producing the
  answer. This closes the arm the `mark_blocked` fix below did not reach. §1.1 item 4, §7.6.

- `run start` reclaims a stranded integration checkout instead of asking the operator to move
  it by hand. A previous run that died before releasing its integration worktree left the
  integration branch checked out, and the next run refused with
  `INTEGRATION_BRANCH_CHECKED_OUT` over state Maestro itself had created. The release verb
  already carried the correct boundary, so `run start` now applies that same predicate before
  refusing: a worktree inside this installation's own run root is reclaimed, and a checkout
  anywhere else — which may be the operator's — is left exactly where it is and still
  refuses, with the refusal now saying so. The decision is path containment against the
  configured run root read from `git worktree list`, never a name or a claim (§1.2). Attempt
  worktrees are untouched, including the blocked ones §8.8 retains for post-mortem.

- Route-admission agent names carry a collision-free discriminator, so a leftover agent from a
  previous run no longer refuses the next capture. The retry keys on the typed error code
  rather than on message text, and a prompt is refused when its target name resolves to
  another pane.

- Byproduct redirection now reaches the agent's own pane. `scratch_env()`'s seven
  variables were passed as `env=` to the `herdr` CLI subprocess, but a pane's shell is
  forked by the herdr server, so the redirect configured the client and died there. The
  harness pre-gate honoured it and the agent's own test run did not, which meant an agent
  that merely ran pytest was convicted under §8.3 clause 4 for the bytecode cache its test
  run wrote. The variables are now forwarded as `--env KEY=VALUE` on `herdr pane split`,
  the one surface that crosses the server boundary, and a launch refuses with
  `LAUNCH_REFUSED:SCRATCH_REDIRECT_MISSING:<keys>` before any pane exists if one is
  missing. `scratch_env()` raises if its key set diverges from the forwarded set, so
  adding a variable for the gates but not for the pane is no longer possible. §8.3's
  permission check is untouched and no ignore list was introduced; this is a fix at the
  redirect tier, which is the order §8.3 requires.

- Blocked transitions record why they blocked. Three arms in the scheduler called
  `mark_blocked` without `detail=`, so a terminal block wrote `{}` and its reason survived
  only as prompt text rendered into the next attempt — prose, which §1.2 forbids as a
  basis for anything. Four of the seven blocked transitions in the live ledger carried an
  empty detail. All three arms now pass a shared typed record carrying the classifier
  reason, the failed clause, the verdict, and the offending paths; the quiescence arm
  already did, so the precedent existed and the budget arms simply never used it.

- Retry guidance accumulates per acceptance surface instead of being overwritten. A single
  string slot held the most recent failure, and the verification arm and the review arm each
  overwrote it, so a node alternating between the §8.3 permission check and the reviewer
  received only the last surface's constraint and dropped the other. Observed in production:
  a node fixed the cache flag, then the clock, then the cache flag again, satisfying each
  constraint individually and never both at once, until its attempt ceiling ended the run.
  Guidance is now a typed ledger keyed by acceptance surface, one entry per surface,
  replaced rather than appended, with every occupied surface rendered into every retry
  prompt. A constraint retires only when its own surface re-evaluates a newer diff — never
  on a pass, which is what caused the regression. Rendering is bounded by a 12,000-character
  budget split evenly across occupied surfaces, elided from each section's tail, with every
  surface keeping its header and an explicit truncation marker.

- A stalled reviewer's reason survives into the ledger. The `ReviewStalled` arm constructed
  a `Classification` with a `reason` the dataclass did not define. The resulting `TypeError`
  was swallowed by the surrounding `except BaseException` and reclassified as a generic
  environmental retry, so the symptom was not a crash but an unexplained retry row with no
  reason recorded. `Classification` now carries an optional `reason`, left outside the
  exactly-one-of pairing `__post_init__` enforces, and the stall reason reaches the durable
  transition row.

- Configuration is resolved from the repository root rather than the process working
  directory. `Path.cwd() / "adws/maestro.config.yaml"` at three resolution sites meant every
  configured command silently required being run from the repository root. Resolution now
  walks up from the invocation directory to the nearest ancestor holding the config file and
  stops at the first `.git` marker, so a nested checkout cannot inherit the enclosing
  repository's plans, state, and route receipts. `plans_dir` still binds to the current
  checkout; only which checkout is selected has changed.

- Plan finalize and run start still refuse when no signed route receipt
  exists. There is no fixture-copy or hand-signed bypass.
- Herdr admission/launch no longer treat the typed composer prompt as a
  receipt. That false success closed the first pane and started a second
  Claude (launch command printed twice).
- Prompts are submitted through the documented coding-agent contract:
  `agent prompt <target> <text> --wait --until <status> --timeout <ms>`, which
  submits the text and its Enter atomically and fails loudly instead of
  reporting success for text left sitting in the composer. `pane run`, which
  types into the pane's shell rather than the agent composer, is gone from the
  launcher. On `agent_prompt_stalled` the recovery is a single
  `agent send-keys <target> enter` followed by `agent wait`; a second
  `agent prompt` would append to the unsubmitted line and send both halves as
  one garbled turn.
- Agent readiness is `agent wait <name> --until idle --timeout <ms>`. The
  launcher no longer polls the undocumented `agent get` field
  `interactive_ready` or scrapes the visible pane for per-agent startup
  banners.
- Marker detection strips the outbound prompt before scanning, so the prompt's
  own copy of the marker cannot be read as the agent's answer.

- First end-to-end production evidence for the whole chain downstream of launch.
  `run start` on `mdl-arch-cmo-extraction` in the lexgenius deployment returned
  `{"blocked": [], "merged": ["lane-wrtop-store-document-tests"], "outcome": "ACCEPTED",
  "run_id": "run-39b5d2a85faa451c907ee835dca3477c"}` — the first node ever merged in that
  deployment. From the lifecycle ledger: attempt 1 launched, ran 52 turns, was rejected by
  review and cancelled with `retry_class=SEMANTIC`; attempt 2 launched, ran 65 turns, reached
  VERIFIED, and merged at `output_sha=6d81ef62fcebf931c955c03f234bac6f366ca3a3`. Review, the
  semantic retry, re-review, the node gate, and the deterministic merge each executed in
  sequence for the first time. One node ran, so this is evidence that the path exists rather
  than that it is robust; §9.8 records the same result with its limits stated.

### Removed

- Dead launcher helpers `_agent_visible_text` and `wait_for_agent_process`,
  along with `wait_for_coder_ui`, the banner-sentinel readiness poll they
  supported.

- `verification.verify_review_node`. It was a second expression of §7.3's five-clause
  review-node predicate and had no production caller: each clause is already enforced by the
  code that owns the observation — `fin.ReviewerReport.model_validate` for parsing,
  `fin.verify_report` for the matrix, `fin.check_occupancy` for the measured window,
  `fin.derive_verdict` with `cr.require_located_findings` for the derived verdict, and
  `fin.ReceiptStore` for the Ed25519 signature — all sequenced by `code_review.review_attempt`,
  whose `ReviewOutcome.passed` is what the scheduler's review branch reads. Two expressions of
  one rule with only one of them running is how the two drift, so the unexercised copy went
  and the enforced one stayed. A comment in its place maps each clause to the code that
  enforces it, and `scheduler_types.py`'s `NodeKind` docstring, which cited the deleted
  symbol as the review node's predicate, now points at the review path instead.
