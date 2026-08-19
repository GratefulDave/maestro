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

## Starting it

```sh
just -g viz
```

`-g` is `just --global-justfile`, so the command works from any directory
without a `cd` and without environment variables. The recipe is one line in
`~/.config/just/justfile` and delegates to `bin/maestro-viz` in this repo,
which is where the logic lives and is version-controlled:

```just
[doc("start the Maestro visualizer (stop | status | <repo>)")]
viz *args:
    @/path/to/maestro/.claude/skills/sssf/apps/visualizer/bin/maestro-viz {{args}}
```

The script starts *both* halves — the Bun API on :4600 and the Vite frontend on
:4601. It frees both ports first, so running it twice leaves one instance
rather than two, and it does not report success until `/api/sources` has
answered 200 through the frontend's proxy. Any other outcome is a non-zero exit
with the failing half's log tail: a missing `bun`, an uninstalled `vite`, a port
that survives SIGKILL, or a process that dies during startup.

`just -g viz stop` stops both halves and frees both ports; `just -g viz status`
reports what is listening; `just -g viz <repo>` runs the API with `<repo>` as
its working directory so that repository's own `adws/maestro.config.yaml` is
discovered (see below). Logs are at `$TMPDIR/maestro-viz/{api,ui}.log`.

Verified against just 1.46.0: `--global-justfile` reads
`~/.config/just/justfile`, which takes precedence over `~/.justfile`. Nothing
is placed on `PATH` and no shell rc file is involved; the recipe carries an
absolute path, so moving this repository means editing that one line.

Do not start the visualizer with `bun run dev`. That script now refuses: it
prints the commands that start both halves and exits non-zero. It used to start
Vite alone, every `/api` request then failed with `ECONNREFUSED`, and the UI
sat on `loading sources…` forever while the startup output looked entirely
successful. `bun run dev:all` starts both but only from the visualizer's own
directory. `bun run dev:ui` is Vite alone, for the two-process form below.

## Running it against a Maestro DAG run

`bun`'s `--cwd` is a flag of the `run` subcommand, so it goes AFTER `run`.
Written the other way round bun prints its usage text and starts nothing:

```sh
bun run --cwd /path/to/maestro/.claude/skills/sssf/apps/visualizer dev:all
```

That form serves every installation recorded in `~/.maestro/registry.json` —
which is most of them — but its working directory is the visualizer, so it
cannot discover a repository's own `adws/maestro.config.yaml`. To have the
target repository's databases discovered from the repository itself, run the
API there and the Vite dev server with `--cwd`:

```sh
cd /path/to/target-repo
bun run /path/to/maestro/.claude/skills/sssf/apps/visualizer/server/index.ts &
bun run --cwd /path/to/maestro/.claude/skills/sssf/apps/visualizer dev:ui
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
it always had. With no tracer database loaded the bare `#/` lands on the first
Maestro ledger's run index instead, and the breadcrumb names that ledger rather
than the tracer's sessions.

Every run in the selected ledger is listed newest first, each card carrying the
plan name (when the plan files are still installed at that digest), the plan
digest, the run id, the live state derived from its node rows, the outcome the
scheduler last declared — `nothing yet` while none has been — and its nodes
counted by state. The list re-reads the ledger once a second, so a run started
after the page loaded appears without a reload; clicking a card opens that run
and the breadcrumb's `<ledger> runs` link returns to the list.

Checks: `bun run typecheck`, `bun run lint`, `bun run test`, `bun run build`.
