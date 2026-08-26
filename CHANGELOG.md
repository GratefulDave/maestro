# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [Unreleased]

### Changed
- **A review-budget grant preserves the blocked attempt.** Semantic `--force` /
  `--grant N` still move the node to `PENDING`. A `REVIEW_BUDGET_EXHAUSTED`
  grant writes `granted_extra_attempts` and a durable recovery marker, and
  leaves the node `BLOCKED` on the same attempt, candidate, repair handoff,
  worktree, actor generation, and lane phase. Only a later `run resume` that
  proves the worktree exists and the prior actor is absent returns that exact
  attempt to the frontier. Resume never allocates a replacement worktree for
  that grant. Typed `workspace_not_found` is absence proof for every pane in
  that workspace; resume invalidates cached lane layouts and recreates the
  workspace under the persisted plan label. Persistent builder/reviewer
  adoption still requires exact pane, cwd, session-path, correlation-token,
  role, and generation. Ordinary late-envelope resume is unchanged:
  `QUIESCENCE_UNPROVEN` stays blocked until an explicit retry.

- **Architecture HTML now shows persistent review plus review-budget grant/resume.**
  Merge waits on a derived `build::review` PASS for the current candidate SHA.
  Semantic `--force`/`--grant` goes PENDING immediately; a review-budget grant
  stays BLOCKED on that exact attempt until absence-proven `run resume` recovers
  it. `workspace_not_found` is absence for every pane.
- **Persistent candidate-review lifecycle replaces inline advisory review.**
  Every reviewable build now projects one derived `build::review` DAG node.
  The scheduler publishes immutable candidate SHAs, reviews each SHA exactly
  once, and requires a matching PASS before merging that exact commit.
  A rejection atomically records typed findings and one repair handoff, then
  returns control to the retained builder session and worktree; the retained
  reviewer waits for the descendant candidate in the same lane tab. Builder,
  reviewer, and retry generations are durable and stale callbacks are audit
  evidence only. Herdr now displays one workspace named for the persisted plan,
  one tab per authored lane, and role/generation pane labels while runtime
  identity remains in SQLite. Candidate reviews, handoffs, actor sessions,
  per-class retry spends, and lane phase are queryable through status/history.
  Terminal cleanup occurs only after merge, block, cancel, or explicit
  liveness-proven abandonment.

- **Architecture HTML brought level with the working-tree runtime.** Dark tokens
  were already correct. The diagrams were not: PlanV3/`TestsNode` were missing;
  `derive_run_state` was drawn as PENDING→RUNNING instead of the first-match
  predicate stack; `Scheduler.run` sent quiescence to ACCEPTED and verify-fail
  to BLOCKED; falsify sat after VERIFIED; repair keyed on review REJECTED;
  the commit chart stopped at two counts; plan validate said 14 obligations;
  worktrees were `runs/run_id`; AttemptRecord was owned by PlanNode; CLI trees
  omitted most subverbs. On-disk plan filename is still `maestro-plan.v1`;
  the visualizer was not changed. Every `docs/**/*.html` now sets CSS
  `color-scheme: dark` on `:root` and `html` (meta alone left the canvas
  light on a light OS).
- **Node code review no longer blocks a merge** (§19 M35). It still runs, its findings still
  reach the retry prompt, the attempt row and the run report, and its verdict no longer fails
  an attempt. `execution.review_ceiling` now bounds how many reviews of one node the scheduler
  pays for rather than how many rejections a node may survive. `BlockReason.
  REVIEW_BUDGET_EXHAUSTED` stays in the vocabulary so `run convergence` can read older ledgers;
  nothing writes it. The record behind the change: `lane-p5-gap-policy` took 39 attempts across
  four `run_id`s with 12 review rejections and zero passes, and in the paused r7 run
  `run-c8910572828c4f5bb5c60c0582dd4be5` attempt 2 was blocked by two findings that both
  restated one complaint about a concurrency hazard the plan asserts nothing about and its
  single-threaded gate could never exhibit.
- **The cross-run budget guard counts SEMANTIC attempts, not review rejections.**
  `run start` / `run resume` refuse `NODE_BUDGET_EXHAUSTED_ACROSS_RUNS` against
  `execution.semantic_ceiling` summed over every prior run of the digest. The refusal payload's
  `cumulative_review_rejections` / `review_ceiling` are now `cumulative_semantic_attempts` /
  `semantic_ceiling`, and the grant-magnitude payload (#81) moved from
  `REVIEW_BUDGET_EXHAUSTED` onto `SEMANTIC_BUDGET_EXHAUSTED` as `semantic_grant_required`.
  `semantic_attempts_total` no longer excludes rows carrying a review-rejection marker: the
  second budget that exclusion protected no longer exists.
- **A repair chain is opened by any SEMANTIC failure that arrives with a proven output commit**,
  not only by a review rejection. `decide_repair`'s first clause keys on the stored output
  commit, which is the thing being repaired.

### Added

- **§7.4's post-work falsification.** After an agent node's post-node gate PASSES and before the
  node is VERIFIED, the scheduler reverts every path the node wrote that its own gate's argv does
  not select, re-runs the gate, and requires it to fail. A node's declared outputs include the
  test file its own gate counts, so `min_cases` cannot tell nine real assertions from nine hollow
  ones; a test file carrying its own copy of the production logic survives that revert and is now
  refused. The refusal is SEMANTIC and carries the sealed output commit, so the next attempt
  repairs the diff rather than re-implementing the node. It reads `node.outputs` and `node.gate`,
  both already present in the shipped `maestro-plan.v2`, so it applies to an already-shipped plan
  with no re-ship. Cost: one further gate run per agent attempt. Where every path a node wrote is
  selected by its own gate the check has no subject; that is reported on the run's hygiene channel
  and not refused.

- `just -g mon` — a terminal run monitor, `.claude/skills/sssf/apps/visualizer/bin/maestro-mon`.
  It answers the questions the visualizer answers — phase depth, node states, attempts, blockers —
  without leaving the shell, and it reads the lifecycle ledger directly rather than shelling out to
  `maestro.py` per refresh, so a poll costs a few SQLite reads and nothing else. `just -g mon`
  watches the newest RUNNING run in the default repo; `just -g mon <run-id>` or `<plan-name>`
  selects one; a second argument sets the refresh interval, default 5 seconds. `MAESTRO_DB` or
  `MAESTRO_REPO` points it at another deployment.

  The entry point is the global justfile recipe `mon`, which is what makes it reachable from any
  directory. That recipe is a one-line shim into this repository, so the body of the thing is
  version-controlled here and reviewable as a diff rather than living in an unversioned justfile —
  the same arrangement `maestro-viz` and its `viz` recipe already use.

- `runtime_sync.py mirror … --apply --commit` — a mirror now copies *and* records itself, so
  bringing a deployment level is one command instead of a mirror followed by a hand-written
  `git add` and `git commit`. The recording is not decoration: `lexgenius-pipeline` carried its
  entire 184-file runtime **untracked** in git, so it changed with no diff and no history, which
  is how it was silently rewritten with stale bytes on 2026-08-19 and nobody could tell.

  It stages **only the paths it wrote**, by name — never `git add -A`, never the runtime
  directory. These are live shared checkouts: while this template's runtime was being mirrored,
  the-library held 24 modified and 11 untracked files elsewhere in its tree, and sweeping those
  into a commit that claims to be a mirror would be its own incident. Anything the operator had
  already staged stays staged and uncommitted, because the commit carries the same pathspec.

  It is also a pre-flight gate. If the destination holds uncommitted work in any file the mirror
  would overwrite — modified since its last commit, or never committed at all — **nothing is
  copied and nothing is committed**, and the refusal names the file and asks for a commit or a
  stash first. That is the case where a mirror destroys the only copy of something, and a
  content comparison cannot see it: bytes on disk say nothing about whether those bytes were
  ever recorded anywhere. A destination that is not a git working tree is mirrored anyway and
  told so plainly, rather than failing the copy.

  The commit message states what did *not* happen: the source revision the bytes came from, the
  counts, and every file held out (deployment-owned or `pinned`), refused, discarded by
  `--overwrite-ahead`, or present only in the destination. A mirror that refused a file did not
  make the trees level, and a message claiming it did would be a false record — which is the one
  thing this project cannot ship. `--commit` requires `--apply`; a plan has nothing to record.
  Nothing is ever pushed: recording locally is recoverable, publishing is not the mirror's
  decision. Still no hook, no post-merge action, no CI job — this makes an explicit invocation
  do its whole job, it does not remove the invocation. Documented in `docs/deployment-drift.md`.

- Deployment drift reports itself. `tests/test_deployment_parity.py` compares this template
  against every deployment declared in `<repository>/.maestro/deployments.json` (or wherever
  `MAESTRO_DEPLOYMENT_REGISTRY` points) on every suite run, closing the half of #70 that a
  person still had to remember. Watching is opt-in by writing that file — no path is hardcoded,
  because the suite runs in CI and on machines that never installed the factory — and a missing
  registry, or a declared deployment that is not on this machine, skips *visibly* through
  `checkout_layout.skip_visibly`, naming the exact path it looked for and how it chose it. A
  registry that is present and malformed fails rather than degrading to "no deployments
  declared": an unknown key is refused by name, because a misspelled `pinned` reading as "no
  exclusions" is precisely the quiet failure this closes. The registry is read from the main
  working tree before the linked worktree, since a registry names roots relative to itself and
  a lane would otherwise resolve `../../lexgenius/adws` to a path no machine has.

  Three findings, kept separate because they call for three different actions: a file present
  in one copy and absent from the other is a deletion (the shape in which 6,009 lines were once
  lost) and keeps its own field; the template being ahead means the deployment is running older
  runtime, and the failure prints the mirror command; the deployment being ahead means work
  exists in one copy only — issue #71 — and the failure offers no command, because reconciling
  in either direction without reading both copies either destroys the only copy or imports one
  installation's local decisions into the shipped runtime. Differences with equal line counts
  are reported with the deployment-ahead finding: "we cannot show the template is newer" is a
  reason to look, never a licence to overwrite. Measured today, `lexgenius-pipeline` is level
  over 185 files and `lexgenius` reports 7 template-ahead and 5 deployment-ahead with nothing
  absent in either direction. Documented in `docs/deployment-drift.md`.

  Nothing mirrors automatically, and deliberately so — no hook, no post-merge action, no CI
  job. A deployment is a live checkout holding other people's in-flight work, and an unprompted
  write into one is the 2026-08-19 `git restore --staged --worktree` incident with a larger
  blast radius. Detection is what is automatic; the write stays a command a person types.

- `runtime_sync.py check-deployments <template> [--registry FILE]` — the on-demand half of the
  same report, for an operator asking rather than a suite failing. It reads the same registry,
  prints the same three findings with the direction split named, exits non-zero when an
  installed deployment has drifted, and never writes. A declared deployment that is not
  installed on this machine is reported and does not count against that exit code.

- `runtime_sync` can be told a path is deployment-owned. `RuntimeCopy.pinned`, populated from a
  registry entry's `pinned` list or the new repeatable `--pin` flag, holds a path out of a
  comparison or mirror in both directions — issue #71's third option, and previously
  inexpressible, so a file a deployment legitimately owns was refused on every future mirror
  and a refusal nobody can clear is a refusal people learn to ignore. Every held-out path comes
  back out in `DriftReport.declared_excluded` and is printed by `describe()` **including on a
  level report**, where it would otherwise be the one case an exclusion never appeared. The
  hardcoded `maestro.config.yaml` exclusion stays distinguishable from a declared one in the
  output. `DriftReport` also now splits its content differences by direction —
  `source_ahead`, `destination_ahead`, `undetermined_direction` — which is what lets the two
  directions fail as different findings instead of one undirected "these files differ".

- `maestro run pause` — the non-terminal operator stop (§7.3, §7.8). It SIGINTs the process that
  claimed the run; `Scheduler` installs a handler for the duration of its loop that latches a pause,
  stops dispatching, quiesces every in-flight worker, and raises `scheduler.RunPaused` instead of
  declaring. `_execute_run` catches it and prints `{"outcome": "PAUSED", …}`. **No lifecycle
  transition occurs**: no outcome is declared, no node row is rewritten, `cancel_requested` stays
  clear, and `latest_outcome` stays NULL — which is the crashed-scheduler shape `run resume` is
  already legal against, so a paused run resumes. That is what makes an OS signal a legal trigger
  under §1.2: the signal starts a process stopping, and the ledger records the same nothing it
  would have recorded had the machine lost power.

  The pid is signalled only when `lifecycle.scheduler_signal_pid` proves identity — the recorded
  `runs.scheduler_start_epoch` compared for equality against the live process's start epoch.
  `scheduler_liveness` alone is a weak witness, because the kernel reuses pids and a stranger
  occupying the number answers alive; an unproven pid is refused with `PAUSE_PID_UNPROVEN` rather
  than signalled. This is that function's first production caller, and its `DEFERRED` row in
  `tests/test_no_dead_seams.py` is deleted with it.

- `maestro run cancel --discard`, and `CancelCause.DISCARDED` behind it. `--discard` is the
  destructive verb and is terminal: it stamps every non-terminal node `DISCARDED` and declares
  `CANCELLED` with that cause, which is deliberately not in `REOPENABLE_CANCEL_CAUSES`, so
  `resume_run` refuses it and `_guard_transition` refuses each node individually. A distinct member
  rather than a reuse of `ABANDONED`, because the two are different facts — every node individually
  adjudicated as work the run should finish without, versus a run thrown away with nothing
  adjudicated — and §1.2 wants that distinction stored rather than inferred. Adding an enum member
  is additive, so ledgers and receipts written before it carry one of the two older values and
  parse unchanged (unlike §19 M21, a field made unconditionally required after the fact).

- `LifecycleStore.adoptable_attempts`, and the unreachable-work warning `--discard` prints from it.
  It names `VERIFIED` nodes and nodes whose latest result was adjudicated `ACCEPTED` without
  merging — work that reached a measured predicate and would simply stop being reachable through
  Maestro. Review-rejected attempts are excluded, filtered on `retry_policy.REVIEW_REJECTED_KEY`
  rather than on anything a reviewer wrote in prose.

- `BlockReason.DECLARED_OUTPUT_UNCOMMITTABLE`, and the two checks that raise it: a declared output
  git will not commit is now a refusal instead of a silent empty success. `inventory()`'s universe
  is `git ls-files --cached --others --exclude-standard`, so a **gitignored** declared output is
  invisible to the measured delta, to §8.3's permission check, and to §8.4's commit at once. The
  node writes the file, the gate can pass over it, the permission check finds nothing to convict
  on, and the attempt commits nothing — the run reports ACCEPTED over an output that never reached
  the tree. Two structural checks close it, and neither reads prose or an agent's claim about its
  own work (§1.2): `worktree.outputs_ignored_in_repo` asks `git check-ignore` about the plan's
  concrete declared outputs at run start, and `worktree.existing_ignored_outputs` compares sha256
  digests of the on-disk paths absent from the after-inventory against the ones recorded when the
  bracket opened.

  The run-start refusal sits beside `_refuse_base_commit_divergence` in `_execute_run`, under the
  same `if resuming: … else:` guard and for the same reason: both are preflights over the plan as
  authored, and the answer here is a function of the declared outputs and the repository's ignore
  rules, neither of which the run itself writes. Refusing on resume could only fire where an
  operator edited `.gitignore` mid-run, and would strand a run whose plan cannot be edited — the
  plan hash is checked at resume — with no repair but abandon. The resumed case is not left
  unguarded: the attempt-settle detector runs regardless of resume and blocks with the same reason,
  paying one attempt for the discovery instead of none. Globs are not put to `check-ignore`, which
  names paths rather than patterns, so a glob that happens to cover ignored files is caught at
  settle.

  The false-positive that made this hard to get right is a provisioned `.venv`: it is gitignored
  *and* legitimately present, and must not convict a node that declared `*.py` and wrote one
  perfectly committable file. `AttemptWorktree.ignored_at_base` — recorded by `take_baseline`
  alongside the baseline itself — is the before-side that separates the two, so only a path the
  attempt created, or whose bytes it changed, is named. Like `baseline`, it is left `None` by
  `reopen_attempt_worktree`, and `existing_ignored_outputs` refuses a `None` before-side rather
  than reading it as an empty one, because an empty before-side attributes an entire provisioned
  dependency tree to the node. Carried with `tests/test_permission_delta_ignored_paths.py`, whose
  new cases include the pre-fix state asserted directly — file on disk, empty measured delta,
  permission check passing — the `.venv` guard end to end through the scheduler, and a run that
  refuses before any scheduler is constructed.

- `runs.scheduler_start_epoch`, and `lifecycle.scheduler_signal_pid` which reads it: recorded
  process identity for the scheduler that claimed a run. `scheduler_liveness` answers only "is
  there a process with this number?", and the kernel reuses pid numbers, so a stranger that
  acquired the pid the scheduler released answers `True` to that question. That is a safe enough
  witness for *reporting* a run — being wrong there only reports the run as it already reads — and
  it is not authority to send the process a signal. The new column records
  `watchdog.process_start_epoch(os.getpid())` beside the pid on all three writes that take
  ownership (`create_run`, `claim_run`, and the resume transition), and `scheduler_signal_pid`
  returns the pid only when liveness holds *and* the live process's start epoch equals the
  recorded one. Because that probe resolves finer than a second, a pid reused inside the same
  second the original process started still reports a different start and reads as unproven; a
  whole-second `started <= scheduler_claimed_at` comparison would have called it proven. Every
  other case returns `None`: no recorded epoch, a platform that cannot answer, a mismatch.
  Unproven is never authority.

  The column is nullable and migrates by `ALTER TABLE`, so existing ledgers keep loading. A `runs`
  row written before it reads `scheduler_start_epoch = None`, still reads live, still derives the
  same run state, and reads as *unproven* rather than proven — the direction that hands no signal
  to whatever now holds the pid. It adds no table and no view, so §10.6's ledger table-set guard is
  unchanged. Carried with the `SignalPidIdentity` cases in `tests/test_run_liveness.py` and two
  backward-compatibility cases over a pre-column ledger.

  This lands the recording and identity half only. The consumer that needs it — a `run pause` verb
  that SIGINTs the claiming process — is not on `main` yet, so `lifecycle.scheduler_signal_pid`
  carries a named `DEFERRED` row in `tests/test_no_dead_seams.py` until it arrives. The row that
  stood there for `watchdog.process_start_epoch` is deleted: that probe now has three production
  callers.

- `tools/runtime_sync.py`, shipped inside the template so it reaches every deployment: the
  supported way to move ADW runtime bytes between checkouts, in place of a hand-run `cp`. `check`
  returns a structured drift report in which **absence is its own field**, separate from a content
  difference — a file present in one copy and missing from the other is a deletion, and that is the
  shape in which 6,009 lines of runtime were once lost without anything noticing. `mirror` plans by
  default and writes only under `--apply`; every write is `shutil.copy2` followed by reading both
  files back and asserting equal sha256, never `git apply`, which no-ops silently on some setups
  while reporting success. It never deletes, and it refuses a destination that is ahead.
  `maestro.config.yaml` is held back whenever either endpoint is a deployment, because lane
  vendors, models and concurrency name a particular installation; it is still compared between the
  two template checkouts. `install.py` and `make_config.py` now copy through the same verified
  primitive, and `test_template_parity.py` delegates its comparison to it, so what the parity test
  fails on is exactly what the mirror repairs. Carried with `tests/test_runtime_sync.py` and
  `tests/test_install_copy_path.py`.

  The "destination is ahead" refusal takes two independent signals, and that is not belt-and-braces
  — it is a measured correction. The mtime signal alone refused **zero** files across both real
  deployments, because a git worktree checkout stamps every source file with checkout time, so the
  template always looks newer than a long-lived deployment. Adding a line-count signal made the
  same survey refuse seven files that hold content the template does not, one of them 91 lines
  longer than the file that would have replaced it.

- `code_review.FindingScope`: the reviewer's third axis, required on every finding from v1 —
  `in_scope` when the fix is an edit to a path the node declared, `out_of_scope` when the only
  edit that answers it writes a path the node was forbidden to write. A blocking, at-threshold
  finding cannot refuse a merge on a forbidden remedy (`GradedCell.rejects`), because a rejection
  whose repair the permission check convicts is a retry loop with no exit; it is recorded instead,
  in `GradedVerdict.unreachable`, `ReviewOutcome.unreachable`, the finding ledger's `scope` and
  `unreachable` columns, and a retry-guidance section that tells the builder not to attempt it.
  `CODE_RUBRIC`'s two unbounded questions —
  `diff.implements_the_stated_instruction` and `diff.gate_is_passed_on_the_merits` — now name the
  declared write scope in the question text, and `ReviewHandoff.render` states the bound directly
  under the paths it bounds. Rubric version moves to `maestro-code-rubric.v2` and the finding
  ledger to `maestro-code-review-findings.v2`, because `review_digest` binds the rubric version
  and a replay must not answer a question that changed under it. Carried with
  `tests/test_review_scope_bounding.py`; §19 M22 records the incident.

- `node.writes_are_sufficient`, a BLOCKING plan-review rubric check asked of every node, and the
  rubric version `maestro-rubric.v3` that carries it. `node.reads_are_sufficient` already asked
  whether a node's agent can do the work from what it is allowed to *read*; nothing asked the same
  question of what it is allowed to *write*. A node's `outputs` are its entire write permission —
  single-producer ownership gives each path to exactly one node and the attempt permission check
  convicts any diff touching an undeclared path — so a node whose instruction can only be
  discharged by editing another node's output is unsatisfiable from its first attempt: the reviewer
  rejects every diff that does not wire production and the permission check would reject every diff
  that does. In run `run-2a44d226e75a4be391a14f02b78a6d25` that cost node
  `lane-p4-enrichment-ordering` eight attempts, six reviews, six builder sessions, a launcher
  failure and a turn timeout before `BLOCKED`/`REVIEW_BUDGET_EXHAUSTED`, on a property of the
  authored bytes that one plan-review cell now settles. The question is phrased over the
  instruction, not over the shape of `outputs`: a node that creates a module nothing at base yet
  imports is the ordinary case and passes whenever a downstream node owns the wiring. Carried with
  `tests/test_node_write_scope.py`, whose control half asserts the legitimate new-module node still
  finalizes PASS.

  No mechanical refusal accompanies it, deliberately. Both structurally decidable signals were
  tested against the incident plan and refuted by it: "a node whose outputs contain no path
  existing at `base_commit`" fires on ten of that plan's twelve nodes and on every legitimate
  new-module node, and "a plan in which no node declares a pre-existing file" does not fire on the
  incident plan at all, because two of its nodes did. `plan_validate.py` is unchanged and the
  twelve obligations are still twelve.

  The version bump adds no receipt key and requires none. `Receipt.from_bytes` still discriminates
  the graded payload shape on the presence of `reject_at` rather than on the rubric label (§19
  M21), so every signed `maestro-rubric.v1` and pre-grading `maestro-rubric.v2` receipt still
  parses, still verifies against its original signature, and still replays with zero reviewer
  launches — asserted directly rather than assumed. The new question is nonetheless mandatory from
  v3 onward rather than optional forever (§3.6 B8): it is a matrix cell, and `verify_report`
  refuses a report that leaves any cell unanswered.

- `maestro._validate_review_clocks`: refuses a review window the remaining live bound cannot hold.
  `reviewer.turn_timeout_s` must be under `reviewer.finalization_timeout_s`, or one silent turn
  consumes the whole review window; `finalization_timeout_s` and `node_timeout_s +
  finalization_timeout_s` must both sit under `execution.backstop_t_s`, or the run-level backstop
  fires inside a healthy review or a healthy sequential node-and-review path. It deliberately does
  not compare `execution.turn_timeout_s` to the review window: after §19 M15 the builder turn clock
  is disarmed once an attempt holds an ACCEPTED result, so the old inequality would have refused
  the shipping template for a closed bug. Carried with `tests/test_review_clock_siblings.py`.
- `maestro._refuse_base_commit_divergence` plus `worktree.resolve_commit`: the single-repo twin of
  `workspace_runtime.prepare_candidate`'s SHA check. At run start — not on resume, where merges are
  supposed to move the head — the integration branch must still resolve to the same commit object
  as the plan's recorded `base_commit`, and an unresolvable recorded base is a refusal rather than
  a fall-through. The single-repo path previously created attempt worktrees against whatever the
  integration branch pointed at and never compared the two (#32). Identity is the resolved commit
  object, so an abbreviated SHA or a tag that names the same commit is admitted. Carried with
  `tests/test_base_commit_enforcement.py`.

- `watchdog.process_start_epoch`, the wall-clock start of a pid, with a Darwin implementation
  reading `proc_pidinfo`'s `PROC_PIDTBSDINFO` at microsecond resolution. A pid is not proof of
  process identity: `os.kill(pid, 0)` answers only that *something* holds the pid, and the
  whole-second clock `ps lstart` exposes cannot separate a process from a reuse of its pid within
  the same second (#37) - so anything that signals or convicts on a recorded pid alone can reach a
  different process than the one it recorded. Linux answers from `/proc/<pid>/stat` at clock-tick
  resolution. On any other platform it refuses, returning `None`, which a caller must read as
  "identity unproven" rather than as "the same process"; returning a coarse or fabricated start
  there would let a reused pid pass for the original, which is the failure the probe exists to
  prevent. Carried with `SignalPidIdentity` in `tests/test_run_liveness.py`. The probe has no
  production caller yet - the scheduler-pid identity check that consumes it has not landed - so it
  is recorded as a named `DEFERRED` row in `tests/test_no_dead_seams.py` rather than left as a
  silent dead seam; landing that caller means deleting the row.

- A one-command launcher for the visualizer, `bin/maestro-viz`, reachable from any directory as
  `just -g viz`. The only invocation that started a working visualizer was `bun run dev:all` from
  the app's own directory; `bun run dev` — the obvious thing to type — is `vite` alone, which
  serves the frontend on :4601 without the API on :4600, so every `/api` request fails
  `ECONNREFUSED` and the UI sits on "loading sources…" while the startup output looks successful.
  The launcher starts both halves, resolves its own paths so no environment variable is involved,
  frees ports 4600 and 4601 first so a second run replaces the first rather than stacking a
  duplicate server, and withholds success until `/api/sources` has answered 200 through the
  frontend's proxy. Every other outcome exits non-zero with the failing half's log tail — a
  missing `bun`, an uninstalled `vite`, a port that survives `SIGKILL`, a process that dies during
  startup — and `vite` runs with `--strictPort` so the printed URL cannot lie about which port it
  landed on. `maestro-viz stop` frees both ports, `status` reports what is listening, and
  `maestro-viz <repo>` runs the API with that repository as its working directory so the
  repository's own `adws/maestro.config.yaml` is discovered. The `just -g viz` recipe is a
  one-line passthrough, which keeps the logic version-controlled here rather than in the user's
  home directory; it reads `~/.config/just/justfile`, verified against just 1.46.0 and verified to
  take precedence over `~/.justfile`, so nothing is added to `PATH` and no shell rc file is
  edited.
- The visualizer's run index is navigable on a Maestro-only server, and its run cards say how far
  along a run is. A bare `#/` there renders the fallback ledger's run index, but the route named
  no source, so the breadcrumb read "sessions" and linked to a tracer route that server does not
  serve, and no tab was marked current while one was plainly on screen; the landing source is now
  resolved once and every piece of navigation points at the view actually rendered. Run cards
  carry the plan digest and their nodes counted by state, because on a fourteen-node plan the dots
  showed the shape of a run without answering how much of it was done.
- Node code review grades a finding's consequence, so a lane can converge on a merge.
  §3.6 A9 forbids gating progress on a zero-finding LLM sweep with restart-on-any-finding, and
  Maestro was doing exactly that: six of `CODE_RUBRIC`'s seven checks are BLOCKING and any
  finding on one rejected the attempt, so acceptance needed an adversarial cross-vendor reviewer
  to find zero defects in a real diff. Over the 27 review reports the production deployment had
  written, `diff.introduces_no_obvious_defect` carried a finding in 25. A finding now carries a
  reviewer-assigned `grade` (`error`/`warning`/`note`) and a `grade_rationale`; a cell rejects
  only when its check is BLOCKING **and** its grade reaches `execution.review_reject_grade`, so
  an ADVISORY check still cannot be escalated by grading it, the reviewer still cannot emit a
  severity or a verdict, and code still derives the verdict (§6.5). The threshold is
  configuration, validated at load, never plan content (§6.2). A finding without a grade, a
  rationale, or a message does not parse (§3.6 B8). Sub-threshold findings are recorded rather
  than discarded, in a ledger beside the receipt under the same subject digest. See §19 M17 and
  §17 item 130.
- A signed finalization receipt carries both halves of what derived its verdict:
  `DerivedCell.grade` and `Receipt.reject_at`, each required by `Receipt.from_bytes`. Both are
  `None` for plan finalization, whose reviewer has no grade to give and whose verdict runs under
  no threshold. They land with the grading rather than after it because §3.6 B8's rule is that a
  field added once receipts exist is optional forever, and a receipt recording a grade without
  the bar it was measured against still records a conclusion nobody can re-derive from it. This
  makes a replayed review whose ledger is missing report the grades and the threshold the
  original review actually used, rather than falling back to severity and to today's
  configuration — the ledger is still written after the receipt, per §1.2, and that ordering now
  costs the rationales alone.
- `maestro plan set-aside` gives a FAIL finalization receipt the operator escape §3.6 B10
  requires, with `maestro plan set-aside-log` reading the escapes back. `ReceiptStore.set_aside`
  retains the FAILed receipt and its original signature under an archival name, writes a signed
  `SetAsideRecord` naming the invoker, the reason and the sha256 of the receipt bytes it
  superseded, and frees the live slot, so the next `finalize` reviews those bytes afresh by the
  same path a `FINALIZATION_STALLED` rerun takes — the absence of a receipt, never a decision
  that reads the record. `finalize` and the replay key are unchanged. The escape needs the
  finalizer's signing key, refuses a PASS, refuses a digest with no receipt, refuses a blank
  invoker or reason, and is reachable only from the verb. Closes #38; discharges §16.3 item 56;
  see §19 M16 and §17 item 132.
- `run cancel` records why it cancelled, and `run resume` reopens the run it stopped.
  `CancelCause` is stored typed at both levels — `runs.cancel_cause` as an attribute of the
  declared outcome, `node_lifecycle.cancel_cause` as its node-level twin — because a run whose
  operator abandoned one lane and then cancelled the run holds both causes at once. `resume_run`
  refuses `ACCEPTED`, refuses a `CANCELLED` caused by `ABANDONED`, refuses a `CANCELLED` with no
  recorded cause, and accepts one caused by `RUN_CANCEL`; on that resume MERGED nodes stay MERGED
  and are never re-executed. Before this, `run cancel` — the operator's only stop control —
  discarded every merged, gate-verified, reviewed node in the run; on the twelve-lane plan of
  `run-75b96fd1f01e46989671771645ee6acc` the same keystroke that stopped the machine threw away
  everything it had landed. Closes #29; see §19 M18 and §17 item 131.
- The Maestro dashboard surfaces `cancel_cause` for a run and for each node, and says whether
  `run resume` will take a cancelled run. The column is read through a schema probe, so a ledger
  older than the migration answers `null` rather than failing the query.

- Plan admission now refuses a plan whose lane cannot satisfy its own contract, and a plan whose
  requirement both prohibits and prescribes the same effect. Two obligations over the
  `plan-contract.v1` IR, evaluated at `plan_contract_ingress.project_draft` — the chokepoint both
  `plan author --from-plan-contract` and `plan ship` cross — emitting typed blockers with JSON
  pointers into the authored IR. A requirement declares a `surface` of `{path, mutation}` records
  (`written` must be one of the owning lane's declared outputs, `inherited` must be produced
  somewhere in that lane's `depends_on` closure, `unmodified` must be a hash-pinned source
  artifact no lane rewrites) and the containment is bidirectional: every declared output must also
  appear in some bound requirement's surface, so a surface copied from the outputs does not pass
  by construction. A plan declares the acts it prohibits once, in
  `extensions.maestro.prohibited_effects`, each with the source document's own words as a
  required `meaning`; each requirement declares a disposition toward each prohibited act
  (`performed`, `planned`, `fake_only`, `none`), and performing a prohibited act is refused, as is
  an act declared `planned` that no requirement in the plan ever executes. Nothing in either
  predicate reads requirement text, verifier oracles, seam descriptions, or fixture meanings; a
  regression test fills every free-text field with prose naming unreachable paths and asserts the
  plan is still admitted. Both obligations require their fields from the first version rather than
  accepting them as optional (§3.6 B8). See §19 M9, M10, M11, M12 and §17 items 122-125.
- The gate runner is resolved from a declared interpreter rather than inherited from the
  scheduler's `PATH`, once, into a type the collector and the gate executors accept in place of a
  bare command, so no dispatch site can construct an unresolved invocation. Capability is proven
  before the run by a probe independent of the plan's selectors, keyed on the runner's own
  documented no-tests-collected exit code — the gate's own exit code cannot distinguish a broken
  interpreter from a selector the lane has not built yet, since both report a usage error and zero
  cases. An unusable runner refuses the run at preflight, with no attempt launched and no
  environmental budget spent. See §19 M8 and §17 item 121.

- `RunReport.review_convergence` — `node_id -> findings-per-attempt`, one count per
  review-rejected attempt in order, surfaced in `maestro run`'s JSON under the same key.
  `review_findings` already said *what* the last reviewer objected to; nothing said whether the
  objections were shrinking, which is the question behind `review_ceiling`. A descending series is
  a node the ceiling cut off early; a flat one is a node more attempts would not have saved. The
  count rides `attempts.extra_json` under `retry_policy.REVIEW_FINDINGS_COUNT_KEY`, on the same
  review-rejected row the budget is already counted from, so `review_convergence_from_attempts`
  rebuilds the series on resume and a run finished by a second process reports the whole run
  rather than its own slice. Nothing transitions on it — it is a count of typed findings under a
  typed key, read only by the report and the JSON (§1.2, §10.1).

### Changed

- The plan-contract projection emits **`maestro-plan.v2`**, and a run refuses anything older with
  `RUN_PLAN_SCHEMA_VERSION_UNRUNNABLE`. Nothing structural changed — `plan_model.PlanV2` subclasses
  the frozen `Plan` and redeclares only its version marker, because v2's obligations *are* v1's.
  What moved is what the projection puts in an agent node's `instruction`: it used to write the
  lane's title and drop `requirements[].text`, so every builder and every reviewer downstream was
  handed a summary of the lane's contract instead of the contract (§19 M26). The fixed projection
  went on emitting `maestro-plan.v1`, which left a plan shipped before the fix indistinguishable in
  version from one shipped after it — a populated field cannot be audited by its consumers, so the
  version string is the only channel that difference can travel on.

  The refusal lives in `_load_runnable_plan`, the one function that turns plan bytes into a plan a
  run will execute, so the coverage claim is a property of the call graph rather than of a list of
  verbs (§19 M6 is the recorded cost of the alternative). It is an allowlist: a version registered
  later and not added to it refuses rather than runs. A v1 plan stays readable, canonical,
  validatable, and finalizable; only running it is refused, and the remedy it names is to re-ship
  the plan from its IR — there is no upgrade function, because the missing requirement text is not
  recoverable from the projected plan.

- `maestro bootstrap` no longer writes the reviewer's HMAC key into `maestro.env`. It writes two
  0600 files into `<state-root>/<repo>/keys/`: `maestro.env`, carrying the verify key, the signing
  seed, and the route verify key — everything author-side work needs and nothing that can make a
  gate refuse — and `reviewer-hmac.env`, carrying the reviewer binding and nothing else. `maestro
  plan gate` refuses while `PLANCTL_REVIEWER_HMAC_KEY` is set, which is correct; what put it there
  was Maestro's own bootstrap, so an operator who sourced the combined file to finalize or start a
  run was then refused on their own plan and had to unset and re-export a variable by hand between
  stages. Nothing in the supported path reads `reviewer-hmac.env` — `plan review` injects the key
  into the `planctl` subprocess itself — and it exists only for driving `planctl review` directly.
  Re-run `maestro bootstrap` to split an existing combined file: `provision_keys` reuses the key
  material it finds, so no key is regenerated and no signed receipt is invalidated.

- The finalization turn clock is gated on route liveness. `_reviewer_window_factory` — the window
  `maestro plan finalize` builds — passed no `actor_status` reader, while `_code_review_runner`'s
  window a few hundred lines below passed one, so every detector behind that seam was unreachable
  on the operator path and `TURN_TIMEOUT` was the only sub-span signal left. It fired
  unconditionally, below a quiescence branch that could never arm, and returned
  `FINALIZATION_STALLED signal=TURN_TIMEOUT after 128.6s` against a reviewer whose Herdr pane was
  still advancing (§19 M30). The factory now supplies raw per-pane `agent_status` — never
  `observe()`, which collapses idle into RUNNING and cannot express "went live, then stopped" — so
  `ACTOR_ABANDONED` and `NEVER_STARTED` are reachable and the turn clock only convicts an actor the
  route has stopped reporting live. `finalization_window.DEFAULT_TURN_TIMEOUT_S` is 900.0 and the
  CLI default is taken from it rather than restated, because two literals for one clock is how a
  raised module default comes to look like it did nothing. The run-side sibling in `watchdog.py` is
  **not** fixed (#107, §16.3 item 59).

- Bare `maestro run cancel` now **pauses** instead of discarding the run. It was the operator's
  only stop control and served two intents — end this run, and stop this run because I am about to
  resume it — with the destructive reading always winning (§19 M18). Ending a run for good is now
  spelled `run cancel --discard`.

- §11.2's liveness backstop quiesces its in-flight workers before it stops. `Future.cancel` cannot
  stop a running worker, and the run loop's `finally` already waits on the pool, so cancelling only
  the futures left the run blocked in `shutdown(wait=True)` until each worker reached its own node
  timeout — the backstop fired and nothing stopped. The declared outcome is `STUCK` either way; the
  backstop's domain is still the run's stopping point rather than quiescence.

### Fixed

- A review rejection now repairs the diff it rejected instead of discarding it (PR #111). A
  rejection classified SEMANTIC recycled the attempt, and the next attempt branched from the
  integration head like any other, so the builder was asked to implement the node again from an
  empty file with the reviewer's findings carried across as text. Consecutive attempts were
  independent implementations rather than iterations of one artifact: `lane-p5-gap-policy` of run
  `fb9973646d344400a9e4f4d7818d00f2` produced 2, 2, 1, and 3 findings across four rejections with
  the same `base_sha` on every attempt row, and the one-finding tree was deleted rather than
  corrected.

  `retry_policy.decide_repair` now selects the base. It is five structural refusals and an
  admission, reading a git object name, a count of durable rows, and members of closed
  vocabularies — never the reviewer's prose. A repair is admitted only when the previous attempt
  was SEMANTIC and carried `review_rejected`, when the commit it stored is exactly what that
  attempt's own ref still publishes, when the integration head has not moved, when the chain is
  under `REPAIR_CHAIN_LIMIT`, and when the last repair did not raise more findings than the
  rejection it repaired. Every refusal falls back to the fresh base used before, so nothing here
  can block a node. `render_guidance` prepends a repair block naming the commit in the tree and
  stating that the work is to be changed rather than written; it renders before the surfaces
  divide the character budget, so truncation never turns a repair prompt into an implement
  prompt. The loop adds no attempts — it changes only what an attempt the review budget had
  already paid for starts from.

- `maestro run convergence` no longer reports a live run as one that already ended (PR #112).
  The verdict partitioned the derived run state into finished and not finished with no third
  answer, so a run between polls printed "not converged — run ended first" beside the live attempt
  rows the same report had just rendered. `lifecycle.run_in_flight` now answers `True`, `False`, or
  `None`, composing the derived state with scheduler liveness rather than adding a third derivation
  of either, and `None` renders as unknown instead of collapsing onto either boolean. A run that
  has not finished reports "not converged yet" with cause `run still in flight`.

- A destroyed Herdr workspace no longer burns the launcher budget on relaunches that cannot
  succeed (issue #79). `HerdrLauncher` memoizes the run's workspace id — deliberately, since that
  is what makes a run's placement a property of the run rather than of wherever focus happens to
  sit — and nothing invalidated it when herdr answered `workspace_not_found`. Once the workspace
  was destroyed mid-run, every subsequent `tab create` named the same dead id and got the same
  refusal, the node spent its LAUNCHER_TRANSIENT budget on relaunches that re-asked a dead
  question, and it blocked `LAUNCHER_BUDGET_EXHAUSTED` — telling an operator a counter ran out when
  nothing was ever retryable. That is verbatim the failure `retry_policy.LauncherFailure`'s own
  docstring describes `DETERMINISTIC_REFUSAL` as existing to prevent; the machinery was built and
  wired, and this refusal simply never reached it.

  The cache is now invalidated at the point the answer arrives. `_tab_create` raises on herdr's
  typed `error.code` — never on message text, per §1.2 — clears the memo when the dead id is the
  one it holds, and `_tab_for` re-resolves once and asks again. The retry is a *different*
  question: `_run_workspace` creates a fresh workspace, so the run continues having spent nothing.

  A workspace that vanishes twice in one launch is refused `TAB_UNRESOLVED` rather than retried
  again. Twice is not a stale cache — something is destroying workspaces as fast as the launcher
  makes them, and a third ask re-poses a question already answered the same way twice.

  `TAB_UNRESOLVED` keeps its non-deterministic classification, which is right for what it names: a
  tab herdr declines to create in a live workspace genuinely may succeed next time. The determinism
  here was never a property of the refusal kind — it was a property of the cache, which is where it
  is now fixed.

- `skip --accept-sha` names an abbreviated SHA as a shape defect instead of reporting it as an
  ancestry failure (issue #78). `worktree.is_valid_output_commit` folds shape, existence and
  ancestry into one boolean — correct for a predicate, useless for a diagnostic — so a SHA that
  failed the canonical-digest regex, before `cat-file` or `merge-base` ever ran, came back as *"is
  not a valid output commit descending from attempt base …"*. In the incident that produced this
  the commit descended from its base perfectly well and `git merge-base --is-ancestor` agreed; the
  only thing wrong with it was seven hex digits instead of forty. The operator was sent looking for
  a history problem that did not exist.

  The requirement is deliberately unchanged: skip records a durable identity, and an abbreviation
  is ambiguous by construction. `worktree.is_object_digest` is now public so a caller that refuses
  a SHA can say which check it failed, and `skip` tests it first, refusing with the defect and the
  one-line remedy (`git -C <repo> rev-parse <sha>`).

  `SkipAncestryRefused`'s docstring is corrected with it. It said the exception meant a SHA that is
  not an ancestor of HEAD; it has always been the refusal channel for every identity check `skip`
  makes — no attempt base, no checked-out branch, not the current HEAD, an unclean worktree — and
  the message is what distinguishes them. That is why a message describing a different failure was
  a defect rather than a wording nit.

- `attempt salvage` reports a declared output git will not commit, instead of committing around it
  in silence (issue #67). A run refuses this at start with `DECLARED_OUTPUT_UNCOMMITTABLE` and the
  scheduler blocks it again at attempt settle; salvage took neither path, so the same node could
  have its stranded work committed under a signed record asserting a digest over what *was*
  committed, while a declared output sat on disk unmentioned.

  **Recorded, not refused**, on the verb's own purpose: a refusal leaves the operator with stranded
  work and no verb, which is the state salvage exists to end. The work is real either way; what was
  missing was a statement of what the commit could not hold. The commit is written and the signed
  record carries `uncommittable_outputs` beside it, printed by the CLI on every salvage.

  The field has **three** states and they are not interchangeable: a list names the paths the commit
  is missing, `[]` says the question was asked and the answer was none, and `null` says it could not
  be asked. A reader that collapses `null` into `[]` turns "unknown" into "clean", which is what the
  field exists to make impossible.

  That third state is why this was not the one-line change the issue proposed. The issue expected
  the before-side to be derivable from the baseline `94cbafb` already records. It is not: the
  baseline's universe is `git ls-files --cached --others --exclude-standard`, and the ignored-at-base
  map is *exactly what that command excludes* — disjoint sets, so no amount of the first
  reconstructs the second. `attempt_baselines` therefore gains a nullable `ignored_json` column,
  written by the scheduler from `attempt.ignored_at_base` at the moment `take_baseline` walks the
  provisioned tree, which is the only moment that walk is possible. The column is additive and
  nullable rather than required, per §19 M21; a ledger written before it existed reads `NULL` —
  "nobody looked" — and never `'{}'`, because reading a missing before-side as empty attributes a
  whole provisioned dependency tree to the node, the false positive `existing_ignored_outputs` was
  built to avoid.

- §7.6's PROCESS_DEAD signal can convict an agent attempt (issue #20). It could not before, and the
  branch had never run for an agent node in the project's history: `watchdog.py` guards it on
  `attempt.pid is not None`, and `attempts.pid` was written from `LaunchHandle.process_group`, which
  is absent for every herdr-spawned agent. Two of the three signals §7.6 names carried the whole
  burden, so an agent whose process died silently waited out the node clock before anything noticed
  — half an hour of a run doing nothing, unattended, with no indication that anything was wrong.

  `herdr pane process-info` reports a foreground process group. `launcher.pane_liveness_pid` reads
  it after the prompt is submitted — earlier and the foreground is still the pane's own shell — and
  the launcher records it on the new `LaunchHandle.liveness_pid`, from where it reaches
  `attempts.pid` and the watchdog's `process_is_alive` check.

  **It is a separate field from `process_group` on purpose, and merging them would be a defect.**
  `process_group` is §8.3's kill target, and §8.3 conditions writing it on an executed §9.8 receipt
  proving the group excludes the pane shell and every sibling attempt. That receipt is now
  registered in §9.8 with its five acceptance criteria (discharging §16.3 item 30, which recorded
  that four sections cited a receipt with no owner) and recorded as **partially executed**: a group
  exists and is distinguishable from the pane shell; sibling exclusion under concurrency and a
  planted survivor the kill terminates are still unrun. Reading whether a group exists and sending
  it SIGKILL fail in opposite directions — a wrong answer on the read path reports a live attempt
  dead, a wrong answer on the kill path terminates the operator's shell — so only the read is
  adopted. §16.3 item 17 stays open, and its budget and attribution costs are unchanged.

  Making the branch reachable also made a question real that could not arise while it was dead, so
  `declared_result_observed` now joins its guard — the same §9.7 rule already applied to the two
  clock signals. A code node was always spared by `exit_status_observed` and an agent node had no
  pid, so no *finished* attempt could ever lose its process here. The measured cost of getting that
  wrong is on record for the code path: a command that exited between two polls was convicted
  PROCESS_DEAD, retried twice into the same race and blocked ENVIRONMENTAL_BUDGET_EXHAUSTED,
  roughly one run in three, for a node that had already succeeded. It is a belt rather than the
  braces — a herdr-spawned agent returns to its composer and idles rather than exiting when it
  finishes a turn, so its foreground group survives and absence really is death for this launch
  path — but it costs one ledger read on an attempt whose process is already gone, and it means no
  route whose agent *does* exit on completion can have accepted work convicted for finishing.

  The same gap had a second and third instance, found by looking for them rather than by waiting:
  both reviewer launch sites in `maestro.py` built their `ReviewerSession` with
  `pid=handle.process_group`, which is `None` for every herdr-spawned reviewer, so the finalization
  window's own liveness check was dead for the same reason. Both now take the fallback.
  `harness_owned_group` deliberately still reads `process_group` alone, because that is the flag
  deciding whether the stall path sends a signal.

  The resolver declines rather than guesses whenever the foreground cannot be told apart from the
  pane's shell — `_available_shell` is true, the group equals `shell_pid`, the group is absent or
  non-positive, or the call fails. Recording the shell's group would make PROCESS_DEAD permanently
  satisfied and never convicting, which is worse than the gap it replaces because it would look
  fixed. A declined group leaves the attempt with exactly the two clocks it had.

- The finalization window arms the reviewer session before its first poll, so §6.5's structural
  signals are the working detector rather than dead code. This is the second instance of the defect
  above and was found by looking for it: `FinalizationWindow.run` opened the window and polled, and
  never called `report_launched`. `poll` returns early on `not session.armed`, so PROCESS_DEAD,
  ACTOR_ABANDONED and TURN_TIMEOUT were all unreachable in production and the span wall clock was
  the **only** detector a stalled reviewer could ever meet — B14's recorded failure exactly, with
  the mechanism written to prevent it switched off. `report_launched` was allowlisted in
  `test_no_dead_seams.py` as having no production caller, which is where the defect had been
  recorded rather than fixed; that entry is now deleted, and the sweep fails if it comes back.

  `test_finalization.py`'s stall assertion changes from `WINDOW_TIMEOUT` to `TURN_TIMEOUT` as a
  direct consequence: an armed reviewer that stops without declaring is now convicted by the signal
  that names what happened, instead of waiting out a clock that names the wrong cause.

  The fix already existed in the `lexgenius` deployment and had never been carried upstream. It is
  one of the eight files tracked in #71, and it was recorded there as an unresolved *semantic
  disagreement* between the copies — the deployment asserting `TURN_TIMEOUT` where the template
  asserted `WINDOW_TIMEOUT`. It was not a disagreement. It was one fix and its test, split across
  two copies, with each half looking arbitrary without the other.

- Precondition waits in `test_participant.py`, `test_scheduler.py` and `test_workspace_runtime.py`
  are hang detectors rather than tuned wall clocks, closing the last residual instances of the
  class `#50` fixed in `test_coordinator.py` (issue #57). Each file now derives every arrival wait
  from one `ARRIVAL_TIMEOUT_S` constant — 60s, overridable with `MAESTRO_TEST_ARRIVAL_TIMEOUT_S` —
  that carries the measurement and the reasoning beside it.

  The distinction the constant enforces is between a wait that is a *precondition* and a bound that
  is a *property under test*. Overrunning a precondition means "the subsystem has not got there
  yet", never "the subsystem is wrong", and its duration is set by real `git` and `pytest`
  subprocess work under contention — this suite's default is `-n auto`, so eighteen workers fork
  subprocesses against one disk while the operator's other work runs alongside. A bound placed at
  roughly the duration it measures is a coin toss whoever measures it, which is why two of the
  bounds replaced here had *already been raised once* by someone who measured, and failed anyway.

  What that cost was not obvious from the failures. `test_simultaneous_repositories_and_targeted_
  cancellation` surfaced as `AttributeError: 'ParticipantExecutionError' has no attribute
  'outcome'` — a participant's 0.8s sleep overrunning a 2.0s run timeout and returning an error
  object where the test expected a result. It reads as a defect in the code under test, and it is
  not one.

  Bounds that assert a property keep their literal values and say so: the
  `assertFalse(cancel_completed.wait(timeout=0.05))` that proves cancellation does *not* complete
  while a launch is held, and the `timeout=0.01` that proves the participant timeout path fires,
  are both deliberately not folded in — expressing either in terms of a hang detector would invert
  it into a sixty-second sleep asserting nothing.

- Retry guidance survives both a second failure from the same surface and the death of the
  scheduler process. `GuidanceLedger` held **one** entry per acceptance surface and `with_*`
  *replaced* it, so a node that failed verification twice carried only the second failure into its
  next prompt; and `Scheduler._guidance` was process-local and rebuilt from nothing, so a resumed
  run dispatched its builder with no guidance at all. Each surface now holds a tuple and `with_*`
  appends; every entry is written to the attempt row that produced it under
  `retry_policy.GUIDANCE_KEY` — by `fail_attempt` on a recycled attempt and by `mark_blocked` on
  the one that hits the semantic ceiling, which is the entry most likely to be wanted, since a
  node blocked on the ceiling is the one an operator resumes — and `guidance_from_attempts`
  rebuilds every node's ledger in `project()`, keyed by `(node_id, base_sha)` as
  `AttemptRecord.guidance_key` defines it. See §19 M23.

  The stated justification for replacement — "newer evidence supersedes older, which is how a
  fixed finding retires" — does not hold. Each surface re-evaluates a *different diff* every
  attempt, so a constraint absent from the latest evaluation may be absent because the attempt
  stopped writing the file it concerned rather than because it was satisfied, and the two are
  indistinguishable from the surface's output.

- `render_guidance` drops a surface's **oldest** entries under budget pressure, not its newest.
  Accumulation makes the rendered guidance grow across retries, which puts B13's overflow mode
  back in play, and `_fit` alone truncates from the end — with history rendered oldest-first that
  would have dropped the finding the next attempt has to fix and kept the one it had already
  addressed. `_fit_surface` drops whole entries from the front, announces the drop, and falls back
  to `_fit` only inside a single oversized entry. The total stays inside
  `GUIDANCE_CHAR_BUDGET`: forty accumulated entries per surface render 11,259 characters against
  the 12,000 cap and the size plateaus rather than climbing, so an accumulating ledger cannot be
  what pushes a handoff past `_preflight_prompt`.

- `docs/plan-authoring.md` no longer implies that `maestro plan review` reads the plan. The step
  described "a second person … reviews the mutated IR", which reads as a semantic review the verb
  performs. It does not: `_plan_review` runs `planctl review` followed by `planctl validate
  --require-approved`, taking the reviewer's configured id and vendor as identity strings, and
  signs a receipt bound to the IR bytes by `ir_sha256`. No model is dispatched, and the receipt
  holds no reading of `surface` or `effects` — those two fields have no independent reader before a
  run begins (#31). The judgement is the reviewer's own, made before they type the command; the
  verb records that it happened and to which exact bytes. Carved from the unmergeable
  `issues/sweep` branch, which had the correction but could not be merged.

- `attempt salvage` no longer signs a receipt for bytes no attempt produced. The verb measured a
  stranded attempt against a baseline it rebuilt with `worktree.inventory_at_commit` — `git
  ls-tree` of the recorded base commit, which is tracked paths only. The measurement bracket's
  real before-side is `worktree.inventory` of the *provisioned* tree, which §8.3 deliberately
  keeps untracked non-ignored content in, so every path an adapter's `provision` or the pre-node
  gate left behind read as a path the attempt had added. Where one of them fell under the node's
  declared outputs — an ordinary glob such as `src/<pkg>/cmo/*.py` covering a provisioned
  `__init__.py` — the permission check passed, `commit_measured_delta` committed the file as the
  attempt's measured delta, and the Ed25519 salvage record asserted its sha256. That is a
  complete, verifiable evidence chain for work that never happened, which is the failure §1.1
  item 4 exists to prevent. The same reconstruction also disabled conjunct (2): a provisioned
  file the attempt rewrote read as `added` rather than `changed`, and a glob is allowed to
  authorize an addition, so tampering with provisioned content was admitted instead of convicted.

  The scheduler now persists the baseline the moment `take_baseline` returns, into a new
  `attempt_baselines` table keyed by `(run_id, node_id, attempt_no)`, with its sha256 digest
  stamped onto the attempt row so the two records must agree before either is believed. Salvage
  reads that record and refuses `SALVAGE_BASELINE_UNRECORDED` when an attempt predates it, rather
  than falling back to the reconstruction — failing open was the defect, not a mitigation of it —
  and `SALVAGE_BASELINE_CORRUPT` when the stored inventory and the stamped digest disagree. The
  refusals are taken before the worktree is reopened, so a refused salvage writes no commit and
  no record. The signed record now also names the `baseline_digest` the delta was measured
  against.

  `worktree.reopen_attempt_worktree` no longer returns the reconstruction in `baseline` at all.
  It leaves the field `None`, so the defect is gone rather than merely unreachable from the one
  caller that overrode it. `tracked_at_base` keeps its derivation from the base commit, which is
  correct — tracking is a property of the commit. `permission_check` and `commit_measured_delta`
  already refused a `None` baseline and now have tests pinning that they fail closed rather than
  reading it as an empty one, together with the control that says why: measured against `{}` the
  delta claims the tracked file and the provisioned file as well as the deliverable, which is
  strictly worse than the substitution it would replace.

- `tests/test_coordinator.py` no longer fails one to six of its cases at random under `pytest -n
  auto`, which is this suite's default (issue #50). Six of its thirty-two cases drive the
  coordinator from a thread and then wait for it to reach a participant dispatch or a global gate.
  Reaching either costs real `git` subprocess work — repository binding, branch creation, candidate
  worktree creation — measured at 0.735s to 1.154s on an idle machine, and each of those waits was
  bounded at 3.0s. Under `-n auto` eighteen workers fork `git` against one disk, that distribution
  moves past the bound, and the precondition reports as a failure: observed here at 8 of 12
  consecutive runs, failing 1, 3, 3, 4, 4, 5, 5 and 6 cases, while `-n 0` passed every time. The
  failing set is not random — it is exactly the six threaded cases, entering in the order of how
  much pre-dispatch work precedes their wait, which is what a load-scaled distribution crossing a
  fixed threshold looks like rather than a race on a shared resource. Every such bound in the file
  now derives from one `ARRIVAL_TIMEOUT_S` constant (60s, `MAESTRO_TEST_ARRIVAL_TIMEOUT_S`
  overrides) documented as a hang detector rather than a latency gate. Nothing the file asserts
  changed: the one wall-clock bound that is a property under test — the 0.5s cancellation bound in
  `test_stuck_participant_cancellation_returns_with_blocked_cleanup_evidence` — is deliberately
  left alone and deliberately not expressed in terms of the constant. This shape had already been
  diagnosed twice in this suite and fixed at one instance each time, in that same stuck-cancellation
  case and in `test_workspace_runtime.py`'s global-gate cancellation case; the siblings left behind
  are what the issue reported.

- Cross-checkout tests resolve their peer repository from git rather than from their own file
  path, so they run inside a linked worktree instead of skipping. `tests/test_template_parity.py`,
  `tests/test_schema_vocabulary_parity.py` and `tests/test_plan_admission.py` each walked up from
  `__file__` to the enclosing repository root and looked for the peer checkout beside it. In a
  linked worktree that root is `.claude/worktrees/<lane>`, so the peer was looked for at
  `.claude/worktrees/the-library`, which exists on no machine, and all twelve tests skipped.
  Every lane authors its template changes in a worktree, which means the parity invariant — the
  only mechanical thing holding the two template copies together — had never once examined lane
  work, and reported nothing while not examining it. The new `tests/checkout_layout.py` asks
  `git rev-parse --git-common-dir` for the main working tree and looks for peers beside that,
  falling back to the old filesystem derivation only when git cannot answer. `test_plan_admission`
  additionally spelled maestro's layout depth into its arithmetic, so it also skipped when the
  runtime under test was the-library's own; it now resolves the-library's checkout by name from
  either side. Skips are no longer silent: each carries the path searched and how that path was
  chosen, and is raised as a `PeerCheckoutMissing` warning so it appears in a default `-q` run
  rather than as an unexplained `s`. The one legitimate skip is unchanged — the peer repository
  not being checked out at all — and a peer that is checked out with its runtime or schema missing
  still fails.

- `docs/plan-authoring.md`: removed the "Not built yet" blockquote over `maestro plan gate`,
  `review`, and `ship`. Those verbs shipped in commit 6707e50 (PR #8) and are registered on the
  `plan` subparser; the binding runbook was still directing plan authors to hand-run the `planctl`
  calls each verb wraps. The earlier release note that recorded the same claim while it was true
  is left as written.

- Signed finalization receipts written before grading existed parse, verify and replay again.
  `rubric_version` names the rubric, not the receipt schema, and the grading fields
  (`Receipt.reject_at`, `DerivedCell.grade`) landed under `maestro-rubric.v2` without moving that
  label, so v2 named two incompatible payload shapes at once. `Receipt.from_bytes` had been
  discriminating on the label — v1 means no `reject_at`, anything else means both new keys are
  required — and therefore refused every pre-grading v2 receipt as failing the frozen schema, with
  its bytes untouched and its signature still verifying. The 110-cell PASS receipt for
  `cmo-consolidation-l-r4` is one of those, and `run start` against its digest could no longer read
  it. The discriminator is now the payload itself: `reject_at` present means the graded shape and
  every cell must carry a `grade`; absent means the pre-grading shape, and both read `None` rather
  than being defaulted. The two shapes cannot be mixed in one payload, so nothing written today can
  omit either field. `maestro-rubric.v1` receipts replay by the same rule. See §19 M21.
- `node retry`, `node skip` and `node abandon` reach a node left `RUNNING` by a scheduler that
  died. All three required `BLOCKED`, and a scheduler that is killed or crashes declares nothing
  and leaves its node `RUNNING`, so the operator's entire escape vocabulary refused the one state
  a dead scheduler produces. The gate now reads the fact it was standing in for: `RUNNING` is
  admitted only when `scheduler_liveness` returns `False`, and refuses `SCHEDULER_STILL_ALIVE`
  when the scheduler is still a process or `SCHEDULER_LIVENESS_UNKNOWN` when liveness cannot be
  said — unknown is not dead. `BLOCKED` is unchanged. The escape closes the node's live attempt
  row in the same transaction as the transition, so the next attempt does not collide with §10.3's
  partial unique index and a late arrival from the abandoned attempt adjudicates `SUPERSEDED`
  rather than `ACCEPTED`. See §19 M19.
- Scheduler liveness compares the short hostname, so a network change no longer strands a run.
  `scheduler_host()` stored `socket.gethostname()` whole, and its domain suffix is a DHCP
  assignment: the same machine recorded `Mac.attlocal.net` on one network and `Mac` on another.
  Raw equality read those as different hosts, `scheduler_liveness` returned `None` permanently for
  every run recorded under the old suffix, and — since an escape fails closed on unknown — those
  runs could not be retried, skipped or abandoned on the only machine that could have recovered
  them. Host identity is now the first DNS label, compared case-insensitively, so a ledger already
  holding an FQDN matches its own short name with no migration. An empty identity still matches
  nothing, and a genuinely different machine still reads unknown. See §19 M20.
- Opening a ledger concurrently no longer fails the `cancel_cause` migration. Two openers could
  each observe the column missing outside a serialized transaction, and the second `ALTER` died
  with `duplicate column name` at `run start` or `run resume` against a ledger predating the
  column. The check and the `ALTER` now share one `BEGIN IMMEDIATE`, and a duplicate-column error
  is read as already-migrated.
- A run whose every reopened node has merged reads `MERGED`, in `run status` and in the dashboard
  alike, instead of a stale `CANCELLED`. `run resume` clears `cancel_requested` but leaves
  `latest_outcome = CANCELLED` standing until the scheduler declares again; during the
  final-acceptance window the live node rows contradict that leftover declaration, and preferring
  the declaration recreated §19 M5 — a finished run reported as cancelled and offered as
  resumable. All-`MERGED` with no live cancel request now derives `MERGED` and `resumable: false`.
  A mix of `MERGED` and `CANCELLED` nodes still reads the declaration, so a run abandoned node by
  node stays `CANCELLED`.
- An interrupted `plan set-aside` resolves to either fully applied or fully absent, and a swapped
  archive is refused. Publication recovers through `_recover_locked`: a complete, signed,
  digest-bound archive and record pair finishes the unlink it was interrupted before; a partial
  pair alongside a live receipt is discarded as an escape that did not happen, so a retry is
  legal. `load_set_aside_receipt` compares the archived bytes against the record's
  `superseded_receipt_sha256`, so an archive substituted after the fact does not read back as the
  receipt the record names.
- `run start` against a digest with no finalization receipt refuses `RUN_RECEIPT_ABSENT` with a
  typed `cause` (`SET_ASIDE` or `NEVER_FINALIZED`) instead of `FILENOTFOUNDERROR`. That
  pseudo-outcome came from `_refusal(type(exc).__name__.upper(), str(exc))`, a shape with four
  operator-visible sites — `_run_start`, `_run_resume`, and `main`'s two last-chance arms — none
  of whose names were declared anywhere or could be branched on. All four now name the condition:
  `_RunRefused` carries a declared outcome plus any discriminator as a typed field,
  `RUN_QUIESCENCE_UNPROVEN` carries the harness's own declared code, `RUN_EXECUTION_FAILED`
  covers what has no better name with the class in `detail`, and `MAESTRO_INTERNAL_ERROR` is
  `main`'s net for a non-workspace verb. `RUN_RECEIPT_NOT_PASS` and
  `RUN_PLAN_NOT_CANONICAL_OR_ELIGIBLE` were declared names carried as `ValueError` messages and
  are now printed as the outcome they always were. See §17 item 133.
- The Maestro dashboard renders a finished cancellation as `CANCELLED` rather than `CANCELLING`
  forever. `cancel_requested` is a request that only a resume clears, and the derived state
  checked it before the node rows; the order now matches `lifecycle.derive_run_state` — a run
  whose every node is absolutely terminal has stopped, whatever any flag says. Closes #39.
- `lifecycle.derive_run_state` reads a declared `CANCELLED` outcome in that same settled branch,
  so a run abandoned node by node — which sets no `cancel_requested` — reads `CANCELLED` rather
  than `QUIESCENT`. That is the only branch where the declaration is read: a resume returns nodes
  to PENDING and un-settles the run, so a superseded declaration can never win there. Without it
  `run status` and the dashboard gave two answers to one question about the same run.

### Changed

- The ADW template's suite runs in parallel by default. `pytest.ini` sets `-n auto`, because the
  suite builds real git repositories and runs real pytest collection in subprocesses — §13.3's
  launcher, worktree and merge negatives cannot be proven against fakes — and that cost is I/O
  bound rather than CPU bound: 1559 collected cases take 22:17 serially and 10:40 under `-n auto`
  at 55% CPU, with identical pass counts. Anything counting collected cases must still pass
  `-o addopts=`, exactly as it already had to in order to clear a `-v` that would cancel `-q`.
- `MAESTRO_architecture.md` records that `maestro plan review` dispatches no model.
  `maestro.py::_plan_review` runs exactly `planctl review` followed by
  `planctl validate --require-approved`, and stamps `reviewer.id` and `reviewer.vendor` from
  configuration into the receipt it writes; the `reviewer:` configuration block's model, profile
  and timeouts drive the node reviewer during a run, not this verb. The receipt is real, signed,
  and bound to the IR bytes by `ir_sha256`. What it does not contain is a model's reading of the
  plan, which contradicts §16.3 item 48's premise that `requirements[].surface` and
  `requirements[].effects` get one independent read before a run, and contradicts what
  `docs/plan-authoring.md`'s instruction to direct the plan reviewer's attention implies to a
  reader. Registered as §16.3 item 55, with item 48 amended to point at it. No code changed.

- **Plans authored before the admission obligations no longer validate, and their approvals are no
  longer reproducible.** `requirements[].surface`, `requirements[].effects` and
  `extensions.maestro.prohibited_effects` are required from their first version rather than
  optional, because a field added later is optional forever (§3.6 B8). An IR authored without them
  therefore fails `planctl validate`: the approved plan `cmo-consolidation-l` returns exit 1 with
  fifteen errors — `extensions.maestro missing: prohibited_effects`, and `requirements[0..13]
  missing: effects, surface` for all fourteen. Its review receipt is **not** invalidated by this.
  The IR bytes are unchanged, they still hash to `390619d5e9917e7562df600a1c473b90fcebc8b608f9286763a6ca61625d1511`,
  that is the `ir_sha256` the receipt binds, and its HMAC verifies. What is no longer true is that
  the approval can be re-derived: the receipt records that a validator returned PASS on those bytes,
  and no validator that exists now will return PASS on them again. Existing approved plans keep
  their receipts and their finalized projections; re-validating one is what fails. A plan that must
  pass the current validator is re-authored under a new name and re-approved, which is the
  supported repair path in any case, since editing an approved IR invalidates its receipt. Recorded
  as §16.3 item 52, because this recurs for every future field that becomes required.

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

- The plan's integration gate is no longer reviewed as though it were a node's gate. `plan_finalization.review_objects` projected it as `ObjectKind.GATE`, so finalization asked it the two node-scoped rubric questions — whether its gate asserts the post-state of "this node's own work" and, BLOCKING, whether its selector "names only what this node's outputs supply". The integration gate has no node and is the one gate in this design that runs the whole suite at the final head (§7.4, §8.8), and its selector legitimately spans tests an earlier plan already merged (§6.4's third executability arm, §19 M14), so the correct answer to the BLOCKING question was the one that fails the plan. `ObjectKind.INTEGRATION_GATE` now carries its own checks: `gate.selector_covers_the_merged_surface` (BLOCKING) asks the inverse question, and `gate.min_cases_is_meaningful` applies to both kinds. Because moving a check between object kinds changes the applicability matrix each receipt persists, the rubric is `maestro-rubric.v2`. See §19 M16 and §17 item 129. Related, and not fixed: a FAIL receipt remains terminal with no operator escape (§16.3 item 56, issue #38).

- A liveness clock no longer convicts an attempt that has already declared a result. The worker
  quiesces the builder agent before it commits the work, runs the post gate, and dispatches the
  cross-vendor reviewer, so from that moment the builder's session transcript cannot grow again by
  construction — while `Watchdog._check_attempt` went on measuring exactly that file and stalled the
  attempt once it had been static for `execution.turn_timeout_s`. An attempt that had written a
  success envelope, been committed by the scheduler, had an adjudicated `results` row written, and
  was sitting legitimately in review was therefore killed and classified `ENVIRONMENTAL`. Measured
  on `run-9e9ac412669140039ae078601048f6c7`: ten reviews, dispatch-to-report latency 46s to 461s,
  and exactly the two that exceeded `turn_timeout_s=300` were killed, while the 260s review survived
  by forty seconds. Latency tracks reviewer turn count at roughly 7s/turn across 15–64 turns, so it
  is unbounded model behaviour rather than a property of those two diffs.

  All three convicting sites now consult the attempt's own declared result first, through a single
  injected `declared_result_observed` predicate that mirrors the existing `exit_status_observed`:
  the turn clock, the node wall clock, and `resume_run`, whose docstring previously described
  discarding an inherited RUNNING attempt as intended design. The predicate reads a new
  `LifecycleStore.result_adjudication` keyed on `(run_id, node_id, attempt_no)`, which projects the
  typed `adjudication` and deliberately omits `payload_json` — §1.2 is enforced by the shape of the
  SELECT rather than by discipline. Only `ACCEPTED` spares an attempt; `SUPERSEDED`,
  `UNKNOWN_ATTEMPT`, and `SHA_MISMATCH` each say the row does not describe the live generation.

  Sparing is not unbounding. A result-holding attempt's wall clock defers to `backstop_t_s` rather
  than being removed, which `SchedulerConfig.__post_init__` already guarantees is finite and
  strictly greater than `node_timeout_s`, so a genuinely hung attempt is still convicted — late, by
  design. The predicate is consulted only once a bound would otherwise fire, so polling a healthy
  attempt costs no ledger read.

  The misclassification also distorted budget accounting in both directions: `ENVIRONMENTAL` and
  `SEMANTIC` debit different budgets, and a discarded rejection never reaches
  `_settle_review_rejection`, so it was never debited against `review_ceiling`. In that run
  `attempts.extra_json.review_rejected` is present on all six correctly-settled rejections and
  absent on all three killed attempts, and `lane-p1-canonical-object-key` merged only because the
  defect silently refunded it a review credit. Correcting the classification makes review budgets
  bite sooner, not later. See `MAESTRO_architecture.md` §19 M15 and §16.3 item 128.

- A gate selector is a set of paths, and three checks read it as a single opaque string.
  `plan_validate._gate_executable` decided the produced arm of `MAESTRO_architecture.md` §6.4 with
  `all(path in produced for path in paths)`, so a selector *mixing* test files an earlier plan had
  already merged with files this run would create matched neither arm and was collected whole at
  its base — where one absent path makes the runner refuse and report zero for the entire
  selector. Measured with `pytest` 9.1.1: an already-merged test file collects 11 cases on its
  own, and 0 when a single not-yet-written path is added to the same invocation. The consolidation
  plan's integration gate — fourteen paths, thirteen produced by its own lanes, one merged
  earlier — was therefore refused at ship with `GATE_EXECUTABLE`, "the integration gate's selector
  collects 0 case(s) at base, below the declared min_cases of 116", and the only repairs available
  to the author were to drop the merged test from the gate or to misdeclare `min_cases`: both
  weaken the evidence the gate exists to produce. The check now partitions rather than choosing
  between two arms. An all-produced selector is still not collected against; an all-existing
  selector is still compared to `min_cases`, with its original message preserved byte for byte;
  and a mixed selector collects only the paths that exist at base and must yield at least one
  case, never comparing a partial base-time count to `min_cases`, which counts what the gate must
  pass *after* merge. Two further instances of the same shape were found by searching for it
  rather than waiting for it, and are fixed in the same change. `scheduler.py` decided whether a
  selector named a path the node produces by testing the *joined* selector string against
  `node.outputs`, which is true only of a single-path selector, so a multi-path node gate whose
  declared output was absent at base classified ENVIRONMENTAL and retried an identically missing
  file until its budget was gone. `worktree.run_node_gate` appended the selector as one argv
  element, so a two-path selector reached the runner as the single path `tests/a.py tests/b.py`,
  which exists nowhere; it had worked for single-path selectors only because the caller's argv
  already carried the selector and the runner deduplicated the repeat. Neither sibling could fire
  on any plan in use, whose node gates all name one path, so both were latent. `plan_model` gains
  `_selector_path_tokens`, `selector_string_paths` and `restrict_selector`; the restricted
  selector is an ordinary `Gate`, so collection still reaches the runner through the single
  `runner_resolution` carrier. Recorded as §19 M14 with the lesson it binds (§17 item 127).

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
