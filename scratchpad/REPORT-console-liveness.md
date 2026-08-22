# REPORT — console liveness (lane L1)

Worktree: `/Users/davidandrews/PycharmProjects/maestro-drilldown`
Branch: `fix/dashboard-drilldown`
PR: maestro #127 (repaired in place)

## What changed

Read-side observation only. No lifecycle write.

`observeAttemptLiveness` now answers from `(pid, attempt_host, attempt_start_epoch)` **and** the attempt's phase:

1. Review window (RUNNING + ACCEPTED result, or `extra_json.review_dispatches`) → **alive**, even when the builder pid is dead. This is the bug.
2. Proven live / proven dead / unprovable stay distinct.
   - no pid → `not_recorded`
   - no host/epoch (old ledger) → `unknown`
   - foreign host → `unknown`
   - start-epoch mismatch → `unknown`
   - matching dead pid → `stale`
   - matching live pid → `running`

`maestroDb` projects `attempt_host` / `attempt_start_epoch` via `optionalColumn` and feeds `results.adjudication === ACCEPTED` into the probe. Dashboard copy no longer claims "pid exists on this host".

## Files touched

- `.claude/skills/sssf/apps/visualizer/server/attemptObservation.ts`
- `.claude/skills/sssf/apps/visualizer/server/attemptObservation.test.ts`
- `.claude/skills/sssf/apps/visualizer/server/maestroDb.ts`
- `.claude/skills/sssf/apps/visualizer/server/maestroDb.test.ts`
- `.claude/skills/sssf/apps/visualizer/shared/types.ts`
- `.claude/skills/sssf/apps/dashboard/lib/types.ts`
- `.claude/skills/sssf/apps/dashboard/app/runs/[sourceId]/[runId]/page.tsx`
- `.claude/skills/sssf/apps/dashboard/app/runs/[sourceId]/[runId]/lanes/[lane]/page.tsx`
- `.claude/skills/sssf/apps/dashboard/app/agents/page.tsx`
- `scratchpad/REPORT-console-liveness.md`

Did not touch `adw_modules/`, `pytest.ini`, or `maestro.config.yaml`.

## Commands

### bun test (visualizer)

cwd: `.claude/skills/sssf/apps/visualizer`

```
bun test v1.4.0 (34cbb9a40)

server/maestroDb.test.ts:
[sssf] cannot read /var/folders/63/_vp95w396gv2kq7r4jd13hsh0000gn/T/maestro-viz-Zo10lr/probe-junk/lifecycle.sqlite3: file is not a database
[sssf] skipping /var/folders/63/_vp95w396gv2kq7r4jd13hsh0000gn/T/maestro-viz-Zo10lr/repo/adws/adw_data/sssf.db — no such file
[sssf] skipping /var/folders/63/_vp95w396gv2kq7r4jd13hsh0000gn/T/maestro-viz-Zo10lr/elsewhere/adws/adw_data/sssf.db — no such file
[sssf] ignoring /var/folders/63/_vp95w396gv2kq7r4jd13hsh0000gn/T/maestro-viz-Zo10lr/corrupt-registry.json: JSON Parse error: Expected '}'
[sssf] skipping /var/folders/63/_vp95w396gv2kq7r4jd13hsh0000gn/T/maestro-viz-Zo10lr/elsewhere/adws/adw_data/sssf.db — no such file
[sssf] cannot read /var/folders/63/_vp95w396gv2kq7r4jd13hsh0000gn/T/maestro-viz-Zo10lr/survivor/junk.db: file is not a database
[sssf] skipping /var/folders/63/_vp95w396gv2kq7r4jd13hsh0000gn/T/maestro-viz-Zo10lr/survivor/junk.db — not an sssf or maestro database

 73 pass
 0 fail
 170 expect() calls
Ran 73 tests across 6 files. [3.20s]
```

Baseline on this branch before the change was **63 pass / 1 fail**. This run is **73 pass / 0 fail**. No new failures. The extra passes are the new decline-reason and review-window cases; the pre-existing failure did not reproduce in this suite.

Acceptance case is in `attemptObservation.test.ts`:
`builder pid dead, attempt in review via ACCEPTED result → alive`
and the maestroDb integration twin:
`dead builder pid in review (ACCEPTED result) reads alive`.

### bunx tsc --noEmit (dashboard)

cwd: `.claude/skills/sssf/apps/dashboard`

```
tsc_exit=0
```

### bun run build (dashboard next)

cwd: `.claude/skills/sssf/apps/dashboard`

```
$ next build
▲ Next.js 16.2.10 (Turbopack)

  Creating an optimized production build ...
✓ Compiled successfully in 34.8s
  Running TypeScript ...
  Finished TypeScript in 41s ...
  Collecting page data using 17 workers ...
  Generating static pages using 17 workers (0/7) ...
  Generating static pages using 17 workers (1/7)
  Generating static pages using 17 workers (3/7)
  Generating static pages using 17 workers (5/7)
✓ Generating static pages using 17 workers (7/7) in 837ms
  Finalizing page optimization ...

Route (app)
┌ ○ /
├ ○ /_not-found
├ ƒ /agents
├ ƒ /analytics
├ ƒ /cost
├ ○ /dispatch
├ ○ /federation
├ ƒ /gates
├ ƒ /inspect
├ ƒ /lanes
├ ƒ /plans
├ ƒ /projects
├ ƒ /runs
├ ƒ /runs/[sourceId]/[runId]
├ ƒ /runs/[sourceId]/[runId]/gates
├ ƒ /runs/[sourceId]/[runId]/inspect
├ ƒ /runs/[sourceId]/[runId]/lanes
├ ƒ /runs/[sourceId]/[runId]/lanes/[lane]
├ ƒ /runs/[sourceId]/[runId]/plan
├ ○ /triggers
└ ○ /workspace


○  (Static)   prerendered as static content
ƒ  (Dynamic)  server-rendered on demand
```

exit 0.

### visualizer vue-tsc

```
vue_tsc_exit=0
```
