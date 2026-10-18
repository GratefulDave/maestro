# ADW runtime changes owed by the operator-console work

Written 2026-08-21. Scope: what `.claude/skills/sssf/templates/adws/**` would need for the
dashboard-port work to be *correct*, as opposed to merely rendered. Nothing here has been
applied — `templates/adws/**` was frozen for the drilldown lane and that freeze held (verified:
`git status --short -- .claude/skills/sssf/templates/adws` is empty on `lane/dashboard-drilldown`).

Line numbers are against this repo's template copy at the time of writing. Re-derive before
acting; the runtime moves.

## Summary

| ID | Defect | ADW change needed? |
|---|---|---|
| A1 | Zero drill-down on /projects /analytics /cost /federation /dispatch /triggers | No — dashboard only |
| A2 | `%253A` double-encoding in every run-page href | No — dashboard only |
| A3 | Dead agents rendered as running | **Yes** — schema |
| A4 | Vendor/model identity absent on /agents | **Yes** — write-side only, no migration |
| A5 | Plan-facts columns already landed here | Mirror owed, not a new change |

A1 and A2 are pure read-side and need no runtime change at all.

## A3 — attempt liveness has no identity

`attempts` (`adw_modules/lifecycle.py:232`) is:

```
run_id, node_id, attempt_no, base_sha, state, started_at,
launched_at, pid, turn_count, retry_class, extra_json
```

It records a pid and **nothing that makes that pid an identity** — no host, no start epoch, no
exit record. The visualizer's `running` was a restatement of ledger state
(`apps/visualizer/server/maestroDb.ts:480`, `running: attempt.state === "RUNNING"`), which is why
three dead `lane-p5-gap-policy` attempts (pids 21697, 72494, 18148) rendered as live agents.

The runtime already solved this one level up, for the run. `runs` carries `scheduler_pid` +
`scheduler_host` + `scheduler_start_epoch`, and `scheduler_liveness`
(`adw_modules/lifecycle.py:2746`) returns **three** answers — `True`, `False`, and `None` for
"no pid recorded, or a pid in another host's namespace". `scheduler_signal_pid` goes further and
requires float-equal start epochs, because "the kernel reuses pids" (#37); a pid reused within the
same second the original started still reads as unproven.

Attempts get none of that. A viewer probing `attempts.pid` is therefore wrong in two directions:

- a pid recorded on another machine reads as local, because there is no host column to compare;
- a reused pid reads as alive, because there is no start epoch to compare.

The three dead pids above read dead *correctly*, but by luck rather than by construction.

**Change:** mirror the run-level triple onto the attempt — `attempt_host`, `attempt_start_epoch`,
additive and nullable — written at `mark_launched` (`adw_modules/lifecycle.py:1343`), plus an
`attempt_liveness()` carrying the same three-answer contract as `scheduler_liveness`.

**Constraint that shapes it:** MAESTRO_architecture.md §1.2. A viewer that infers death from a pid
probe must **not** write a corrected state back to the ledger. It observes and reports the
disagreement; it never resolves it. So the deliverable is a *reader* plus the columns that make the
read provable — never a reconciler.

The read-side lane took the interim route (`apps/visualizer/server/attemptObservation.ts`,
`attemptIdentity.ts`), shipping a `liveness` enum of
`running | stale | not_recorded | not_running` beside a boolean `running`, probed with
`process.kill(pid, 0)` — `EPERM` read as alive, `ESRCH` **and anything else** read as dead. It
correctly reports the three known dead pids as `stale` and stops counting them as Running.

That enum has no fourth state for *unprovable*, which is exactly what the missing columns would
have supplied. Two rows are therefore misreported and cannot be detected from the read side:

- a pid recorded on **another host** is probed against this machine's process table, so an
  unrelated local process makes a remote attempt read `running` and an absent one makes it read
  `stale`. `scheduler_liveness` returns `None` in this case, on purpose;
- a **reused** pid reads `running`, because there is no recorded start epoch to compare — the
  case `scheduler_signal_pid` refuses to act on (#37).

Both collapse into a confident answer where the run-level code says "cannot be said". Adding the
two columns is what lets `liveness` gain its `unknown` member honestly.

## A4 — vendor/model is known at dispatch and thrown away

`/agents` claims "Vendor and model identity are not in the ledger". True of the `attempts` table,
false of the runtime: `launcher.py:401` declares `LaunchSpec.model` and passes it to the binary as
`--model` at `launcher.py:597`; vendor lives in `permissions.py` for the cross-vendor reviewer
check; and the deployment's `maestro.config.yaml` declares `execution.model xai-oauth/grok-4.6` /
`vendor xai` and `reviewer.model openai-codex/gpt-5.6-sol` / `vendor openai`. The attempt row
records none of it.

**Change, minimal:** `mark_launched` already takes `extra: Optional[Mapping]` and merges it into
`extra_json`. Pass vendor/model/route through it. No migration, no schema change.

This is an established, tested pattern rather than an unused hook. Call sites, from
`lsp_find_references` **plus** grep (the language server misses the two scheduler sites because
`store` is untyped there — neither tool alone gives the full set):

| Site | Passes `extra=`? |
|---|---|
| `maestro.py:3520` | yes — `{watchdog.SESSION_PATH_KEY: str(handle.transcript_path)}` |
| `adw_modules/scheduler.py:1301` | no — pid only |
| `adw_modules/scheduler.py:1353` | no — pid only |
| `tests/test_lifecycle.py:150` | yes — covers the `extra` path |
| `tests/test_lifecycle.py:153`, `tests/test_step4_ledger.py:235,254` | no |

`session_path` already rides this field — the same file the read-side workaround parses for
`{"type":"model_change","model":"xai-oauth/grok-4.6"}` records. Parsing it per attempt costs I/O on
a page that already loads 135–139 attempts across every registered source.

**Change, stronger** (matches the plan-facts precedent already landed on this branch): additive
nullable `agent_vendor` / `agent_model` / `agent_route` columns plus a backfill verb reading the
session jsonl, in the shape of `maestro.py run backfill-plan-facts`.

The drilldown lane confirmed the need independently and stopped at the boundary: it renders
`model` with a `model_source` of `observed` (last `model_change` in the first 64 KiB of
`session_path`) or `declared` (the `maestro.config.yaml` role block), and reports **observed vendor
as `not_recorded` on nearly every attempt** — because the jsonl record is
`{type, model, resolvedModelIsFallback}` and carries no vendor key at all. So the read side can
recover the model but structurally cannot recover the vendor. Only a launch-time write can.

Either version must keep NULL ("this ledger predates the column") distinguishable from empty
("nothing was declared"). House style: `optionalColumn`, and the `merge_cause` / `ignored_json`
comments.

## A5 — the mirror already owed

The plan-facts columns that landed on `lane/dashboard-port` make `tests/test_template_parity.py`
fail against the-library, and deployment parity fail against lexgenius and lexgenius-pipeline. Not
mirrored; the parity test was **not** edited to hide it.

Sequencing hazard: mirroring into a deployment holding shipped `maestro-plan.v1` plans makes them
unrunnable (`RUN_PLAN_SCHEMA_VERSION_UNRUNNABLE`, from #104) until re-shipped from IR, and that
cannot be done mid-run. Sequence against what is actually running there.

## If these are implemented while the freeze stands

Per the standing amendment: never modify an existing template file. Write the proposed runtime
change as a separate, clearly-named file beside the original — e.g.
`adw_modules/lifecycle.proposed-attempt-liveness.py` — with a companion `.md` of the same basename
covering what it changes, why the read side could not cover it, the exact diff, the migration
implied, and what must be verified. Nothing live may import the proposal.

## Related

- `MAESTRO_architecture.md` §1.2 (no transition from prose), §7.6 (launch/heartbeat signals)
- `CLAUDE.md` — the three runtime copies, and `tools/runtime_sync.py` as the only sanctioned mirror
- `.claude/worktrees/lane-drilldown/LANE_REPORT_drilldown.md` — the read-side lane's own account
