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

### Fixed

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

### Removed

- Dead launcher helpers `_agent_visible_text` and `wait_for_agent_process`,
  along with `wait_for_coder_ui`, the banner-sentinel readiness poll they
  supported.
