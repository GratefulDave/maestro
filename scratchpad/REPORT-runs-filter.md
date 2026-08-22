# REPORT — runs display filter

Worktree: `/Users/davidandrews/PycharmProjects/maestro-runfilter`
Branch: `feat/runs-filter-barren`

Display filter only. No delete verb. Ledger opened `mode=ro`. D1 zombie not special-cased.

## What landed

- `lib/runVisibility.ts` — `shouldHideBarrenRun`: hide iff merged==0 AND not in-flight AND not `needsAttention` AND not `?all=1`
- `app/runs/page.tsx` — URL `all=1`, banner when hideable count > 0, stats stay on listed (unfiltered) counts
- Detail route `/runs/{source}/{runId}` untouched
- Tests: `lib/runVisibility.test.ts`

## Predicate vs this ledger

Rule 4 uses existing `isInFlight` / `needsAttention`. On the live pipeline ledger **0 of 6 zero-merge runs are hideable**:

| run | API state | outcome | merged | why visible |
|---|---|---|---|---|
| `75b96fd1…` | CANCELLED | CANCELLED | 0 | needsAttention |
| `fb997364…` | CANCELLED | CANCELLED | 0 | needsAttention |
| `7034bdf9…` | BLOCKED | BLOCKED | 0 | needsAttention |
| `774cb496…` | RUNNING | null | 0 | isInFlight (D1 zombie; not special-cased) |
| `c8910572…` | RUNNING | null | 0 | isInFlight (same shape as D1; not special-cased) |
| `9f76fa05…` | BLOCKED | BLOCKED | 0 | needsAttention |

Visible set includes D1 zombie `run-774cb4967117` until D1 lands. `run-c8910572828c` is the same RUNNING-with-dead-looking-outcome shape; also left visible.

Banner `"N runs with no merged nodes hidden"` is absent on this ledger because N=0. Control appears when a finished non-attention barren run exists. Tests cover that path.

## Ledger row counts — identical

Opened: `file:$HOME/PycharmProjects/.maestro-state/lexgenius-pipeline/lifecycle.sqlite3?mode=ro`

```
# before
runs|11
dag_nodes|88
node_lifecycle|88
attempts|129

# after (same command)
runs|11
dag_nodes|88
node_lifecycle|88
attempts|129
```

## Tests

```
$ bun test lib/runVisibility.test.ts
bun test v1.4.0 (34cbb9a40)

 7 pass
 0 fail
 17 expect() calls
Ran 7 tests across 1 file. [30.00ms]
```

Covered: zero-merged finished hidden; zero-merged in-flight shown (RUNNING/PENDING/CANCELLING); needs-attention shown; one merged shown; `?all=1` keeps all 11 fixtures.

## tsc / build

```
$ bunx tsc --noEmit
(exit 0, no output)

$ bun run build
▲ Next.js 16.2.10 (Turbopack)
✓ Compiled successfully in 9.6s
  Running TypeScript ...
  Finished TypeScript in 6.6s ...
✓ Generating static pages using 17 workers (7/7) in 468ms
ƒ /runs
ƒ /runs/[sourceId]/[runId]
(exit 0)
```

`**/*.test.ts` excluded from Next `tsc` so `bun:test` types do not fail the app check.

## Live pages (API :4600, dashboard :4318)

Source id `maestro:lexgenius-pipeline` (`maestro%3Alexgenius-pipeline`).

Published-runs stat = **11** on `/runs`, `/runs?source=…`, and `/runs?source=…&all=1`.

Unique run ids on each list page = **11**. `run-card` count = **14** because the 3 in-flight runs also render in "All runs" (pre-existing duplication).

| path | status | published | inflight | attention | unique run ids | hidden banner |
|---|---|---|---|---|---|---|
| `/runs` | 200 | 11 | 3 | 7 | 11 | no |
| `/runs?source=maestro%3Alexgenius-pipeline` | 200 | 11 | 3 | 7 | 11 | no |
| `/runs?source=…&all=1` | 200 | 11 | 3 | 7 | 11 | no |
| `/runs/…/run-c8910572828c…` | 200 | n/a | n/a | n/a | detail renders | n/a |
| `/runs/…/run-774cb4967117…` | 200 | n/a | n/a | n/a | detail renders | n/a |

GET `/api/sources` → one source `maestro:lexgenius-pipeline`. GET `/api/sources/:id/runs` → 11 rows.

## Files touched

- `.claude/skills/sssf/apps/dashboard/lib/runVisibility.ts` (new)
- `.claude/skills/sssf/apps/dashboard/lib/runVisibility.test.ts` (new)
- `.claude/skills/sssf/apps/dashboard/lib/api.ts` (re-export moved helpers)
- `.claude/skills/sssf/apps/dashboard/app/runs/page.tsx`
- `.claude/skills/sssf/apps/dashboard/components/SourceBanner.tsx` (`detail` accepts ReactNode)
- `.claude/skills/sssf/apps/dashboard/package.json` (`test` script)
- `.claude/skills/sssf/apps/dashboard/tsconfig.json` (exclude tests)
- `scratchpad/REPORT-runs-filter.md` (this file)

Not committed: `bun.lock` from local `bun install`. Not touched: `templates/adws/**`, markdown docs, ledger, plan dirs.

## Commands run

```
git status -sb
sqlite3 'file:…/lifecycle.sqlite3?mode=ro'  # schema + counts + merge join
bun install   # dashboard, then visualizer (local node_modules only)
bun test lib/runVisibility.test.ts
bunx tsc --noEmit
bun run build
hub start viz-api PORT=4600
hub start dash-4318 -p 4318 MAESTRO_API_PORT=4600
# fetch API + HTML counts via eval
```

No `run start` / `run resume`. No ledger writes.

# Follow-up — D1 + filter retune (2026-08-22)

## Task A — run scheduler liveness

Read-side only. `observeRunLiveness` mirrors #127: `running` / `abandoned` /
`unknown` (plus `not_running` when liveState is already terminal). Never a
bare boolean. Unknown is not dead.

`run-774cb49671174be9a6862de721da1394` API detail:

```
state: ABANDONED
scheduler_liveness: abandoned
declared_outcome: null
```

No ledger write. Counts after still `11 / 88 / 88 / 129`.

Tests: `server/runObservation.test.ts` (10) + maestroDb scheduler describe
(dead → abandoned, live → RUNNING, null fields → unknown, foreign host →
unknown, epoch mismatch → unknown, real 774cb → abandoned). Full visualizer
suite `89 pass / 0 fail`.

## Task B — hide terminal barren

`shouldHideBarrenRun`: hide zero-merge when not in-flight, including
declared CANCELLED/BLOCKED. Genuinely in-flight (RUNNING/CANCELLING/PENDING)
stays. `?all=1` still shows all. Direct detail links still render.

## Live ledger predicate (API GET /api/sources/maestro:lexgenius-pipeline/runs)

total 11 · hidden 6 · shown 5 · `?all=1` 11

| run | state | liveness | outcome | merged | hide |
|---|---|---|---|---|---|
| `c0523695` | MERGED | not_running | ACCEPTED | 6 | no |
| `9f76fa05` | BLOCKED | not_running | BLOCKED | 0 | **yes** |
| `3fcd8c75` | MERGED | not_running | ACCEPTED | 5 | no |
| `c8910572` | ABANDONED | abandoned | null | 0 | **yes** |
| `774cb496` | ABANDONED | abandoned | null | 0 | **yes** |
| `7034bdf9` | BLOCKED | not_running | BLOCKED | 0 | **yes** |
| `fb997364` | CANCELLED | not_running | CANCELLED | 0 | **yes** |
| `2a44d226` | ABANDONED | abandoned | BLOCKED | 7 | no |
| `75b96fd1` | CANCELLED | not_running | CANCELLED | 0 | **yes** |
| `9e9ac412` | CANCELLED | not_running | CANCELLED | 1 | no |
| `0120c320` | BLOCKED | not_running | BLOCKED | 1 | no |

Dashboard HTML:

- `/runs?source=maestro%3Alexgenius-pipeline` — Published 11, In flight 0,
  banner `6 runs with no merged nodes hidden`, 5 cards, 774 absent
- `?all=1` — 11 cards, banner `6 … shown`, 774 present
- `/runs/…/run-774cb4967117…` — 200, renders, `abandoned`

`2a44d226` is another dead-scheduler run (7 merges) — shown because not barren.

Ledger after: `runs|11 dag_nodes|88 node_lifecycle|88 attempts|129`

Dashboard tests: `11 pass`. `bunx tsc --noEmit` exit 0. `bun run build` exit 0.

