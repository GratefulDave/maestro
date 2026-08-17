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
| `sources.ts` | the registry: probes each database for its tables and builds the right reader |

Neither runtime writes the other's schema. To add a third ADW: write a reader,
add a `SourceKind` in `shared/types.ts`, add a probe row to `PROBES` in
`sources.ts`, and add a view keyed on that kind in `src/App.vue`.

Every source is opened read-only. `SssfDb.setArchived` remains the only write
in the process; the Maestro reader has no write path at all, because a
lifecycle row is run authority rather than review triage.

`server/maestroDb.test.ts` builds its fixtures from the runtime's own `SCHEMA`
literal in `templates/adws/adw_modules/lifecycle.py`, so a schema change breaks
the test instead of silently emptying the dashboard. Run it with `bun test server`.
