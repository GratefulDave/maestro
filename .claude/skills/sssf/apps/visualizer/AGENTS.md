<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-08-15 | Updated: 2026-08-15 -->

# visualizer

## Purpose
Vue/Vite frontend with Bun server for browsing Maestro sessions, phases, traces, stats, and model metadata.

## Subdirectories
| Directory | Purpose |
|-----------|---------|
| `server/` | Bun server and DB access (see `server/AGENTS.md`) |
| `src/` | Vue UI and TypeScript helpers (see `src/AGENTS.md`) |
| `shared/` | Shared API/domain types (see `shared/AGENTS.md`) |
| `public/` | Static assets and model images (see `public/AGENTS.md`) |

## For AI Agents
Maintain shared types across server/client. Verify UI behavior against the running app; use package scripts for checks.

<!-- MANUAL: Any manually added notes below this line are preserved on regeneration -->

## Running it against a Maestro DAG run

From the target repository (the one holding `adws/maestro.config.yaml`), both
its tracer database and its Maestro lifecycle ledger are discovered:

```sh
cd /path/to/target-repo
bun --cwd /path/to/maestro/.claude/skills/sssf/apps/visualizer run dev:all
```

Or name the ledgers explicitly — `--db` is repeatable and each database is
probed for the schema it actually holds:

```sh
bun run server/index.ts \
  --db ~/PycharmProjects/.maestro-state/lexgenius/lifecycle.sqlite3
# MAESTRO_PLANS=<repo>/.maestro/plans names the runs; the ledger stores only digests.
```

With more than one database loaded the topbar grows a tab per source. A Maestro
source shows its runs at `#/s/<source id>` and one run at
`#/s/<source id>/<run id>`; the tracer's session view keeps the bare `#/` routes
it always had.

Checks: `bun run typecheck`, `bun run lint`, `bun run test`, `bun run build`.
