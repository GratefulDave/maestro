<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-08-15 | Updated: 2026-08-15 -->

# server

## Purpose
Bun server endpoints and SQLite/session data access for the visualizer.

## For AI Agents
Keep API responses aligned with `shared/types.ts`; protect DB lifecycle and error handling.

<!-- MANUAL: Any manually added notes below this line are preserved on regeneration -->

## Two schemas, one dashboard

| File | Reads |
|------|-------|
| `db.ts` | the SSSF tracer's `sssf.db` — sessions / phases / events |
| `maestroDb.ts` | Maestro's `lifecycle.sqlite3` — runs / dag_nodes / node_lifecycle / attempts |
| `artifactFactoryDb.ts` | the artifact-factory ledger — runs / dag_lanes / lane_state / artifacts |
| `stepLog.ts` | `steps.jsonl` beside whichever ledger — the scheduler's step narration |
| `sources.ts` | the registry: probes each database for its tables and builds the right reader |

## The step log

`GET /api/sources/:id/runs/:run_id/steps?after=<byte offset>` reads
`steps.jsonl` from the ledger's own directory — derived, never configured,
because the scheduler writes it there.

It exists because the ledger records a stage change when it *completes*. A lane
sits on `REVIEWING_CODE` for minutes while a tree is provisioned, a sealed suite
runs and a reviewer is dispatched, and on a ledger-only dashboard that is
indistinguishable from a lane that has stopped — an operator reported a
mid-provision lane as "doing nothing". These lines are what happens in between.

It is **narration, not authority**. No field here decides a stage, the dashboard
never writes the file, and where a step disagrees with `lane_state.stage` the
ledger is right.

Three properties the reader exists for, all covered by `stepLog.test.ts`:

- the cursor is a **byte offset** and advances over whole lines only, so a
  reader that catches the writer mid-append leaves the fragment for the next
  poll instead of emitting or losing it;
- a line that does not parse is skipped, never raised — one malformed record
  must not cost the operator the whole feed;
- `has_more` means "the read cap cut this page short", not "bytes remain past
  the cursor". Those differ by exactly the line being appended, and conflating
  them has the client re-request the same nothing at full speed.

A file that does not exist answers `present: false`, which the UI renders as a
stated fact. It is the ordinary state of a run whose scheduler has not narrated
anything, and 404 would be indistinguishable from a run that has gone away.

Neither runtime writes the other's schema. To add a third ADW: write a reader,
add a `SourceKind` in `shared/types.ts`, add a probe row to `PROBES` in
`sources.ts`, and add a view keyed on that kind in `src/App.vue`.

Every source is opened read-only. `SssfDb.setArchived` remains the only write
in the process; the Maestro reader has no write path at all, because a
lifecycle row is run authority rather than review triage.

`server/maestroDb.test.ts` builds its fixtures from the runtime's own `SCHEMA`
literal in `templates/adws/adw_modules/lifecycle.py`, so a schema change breaks
the test instead of silently emptying the dashboard. Run it with `bun test server`.
