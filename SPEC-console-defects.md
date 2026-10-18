# Spec — console defects found operating the dashboard against a real ledger

**Two live items: D1 and D3. D2 is withdrawn — it was a wrong finding, kept below with the
mistake that produced it.**

Written 2026-08-22 against maestro `main` @ `f89e0bf`. Each finding below was reproduced
against a real ledger, not read off the code. Evidence commands are included so you can
re-derive rather than trust this file.

## Read first

`CLAUDE.md` and `MAESTRO_architecture.md` §1.2 bind this work. §1.2: no lifecycle
transition may be caused by pane text, prompt text, a free-text envelope field, or an
agent's claim about its own work. **D1 and D3 below are read-side observations and must
stay that way** — the console may *display* that a scheduler looks dead; it must never
write a terminal outcome or drive a transition from that observation.

The runtime exists in three copies. `tools/runtime_sync.py` is the only tool that may move
bytes between them — never `cp`, `rsync`, or `git apply`. D3 touches the runtime, so it
must be mirrored to the-library and the parity test kept green. D1 touches only
`.claude/skills/sssf/apps/`, which exists in this repo alone and has no mirror.

## Reproduction environment

```bash
# API (port 4600)
cd .claude/skills/sssf/apps/visualizer
MAESTRO_DB=~/PycharmProjects/.maestro-state/lexgenius-pipeline/lifecycle.sqlite3 \
  MAESTRO_PLANS=~/PycharmProjects/lexgenius-pipeline/.maestro/plans \
  PORT=4600 bun run server

# Dashboard (port 4317), second terminal
cd .claude/skills/sssf/apps/dashboard
bun install
MAESTRO_API_PORT=4600 bun run dev
```

That ledger predates #128, so its `attempts` table has **no** `attempt_host` and no
`attempt_start_epoch`. That is a feature for testing: it exercises the old-ledger decline
path. Do not "fix" it by migrating the ledger.

---

## D1 — a run whose scheduler is dead still reads as running

**Severity: high.** This is the same defect class as #127, one level up.

### Evidence

`run-774cb49671174be9a6862de721da1394` displays as running. It is not.

```bash
DB=~/PycharmProjects/.maestro-state/lexgenius-pipeline/lifecycle.sqlite3
sqlite3 -header $DB "select run_id, latest_outcome, scheduler_pid, scheduler_host,
                            scheduler_start_epoch
                       from runs where run_id like 'run-774cb%';"
# latest_outcome empty, scheduler_pid 3368, scheduler_host Mac,
# scheduler_start_epoch 1787214672.58935

ps -p 3368            # PID 3368 NOT RUNNING
hostname -s           # Mac  -- same host that recorded the pid

sqlite3 $DB "select state, count(*) from node_lifecycle nl
               join dag_nodes d on d.node_id = nl.node_id
              where nl.run_id like 'run-774cb%' group by state;"
# PENDING 36, RUNNING 9
```

Its scheduler was killed, so nothing ever wrote a terminal outcome. The ledger says
running and the console reports that faithfully.

### Root cause

`server/maestroDb.ts:341` `runRows()` selects `cancel_cause` and `scheduler_host` as
optional columns and **never selects `scheduler_pid` or `scheduler_start_epoch`**. The run
projection therefore has no way to observe that the scheduler is gone.

The evidence is already in the ledger. `adw_modules/lifecycle.py:915` writes all three at
run creation: `os.getpid()`, `scheduler_host()`, `wd.process_start_epoch(os.getpid())`.

### Required change

Give the **run** the reasoning that #127 gave the **attempt**. `server/attemptObservation.ts:207`
`observeAttemptLiveness` is the shape to mirror — including its decline vocabulary.

A run must resolve to one of at least three answers, never a bare boolean:

| condition | answer |
|---|---|
| no terminal outcome, scheduler pid alive on this host, start epoch matches | running |
| no terminal outcome, scheduler pid dead on this host, start epoch matches | **abandoned** (the bug) |
| recorded host is not this host | unknown — a pid is meaningless off its host |
| `scheduler_pid` / `scheduler_start_epoch` absent (old ledger row) | unknown, **not** dead |
| start epoch does not match the running process | unknown — pid reuse |

"Unknown" and "abandoned" must be distinguishable to an operator. Collapsing unknown into
dead is the §1.2 violation this whole family of fixes exists to prevent — it convicts a
run on evidence the ledger does not carry.

### Acceptance

- A unit test where the scheduler pid is dead, the host matches, the epoch matches, and no
  terminal outcome exists → the run reads abandoned, not running. Without this test the
  change has not been made.
- A test per decline reason: absent columns, foreign host, epoch mismatch.
- A test that a live scheduler still reads running — the fix must not convict live runs.
- `MaestroRunSummary` (`shared/types.ts:490`) carries the new field, and a page renders it.
  A field with zero readers is a build failure (B15) — #128 shipped one and had to fix it.
- The console still **displays only**. No write path, no transition.

---

## D2 — WITHDRAWN. Not a defect; the finding was wrong.

**This item was retracted 2026-08-22 before any work started on it.** It is kept rather
than deleted so nobody re-derives it from the same mistake.

The original claim was that `components/NodeDagView.tsx` had zero importers and no route
mounted it. That is false. It is mounted on **two** routes:

- `app/runs/[sourceId]/[runId]/page.tsx:75` — run detail
- `app/runs/[sourceId]/[runId]/plan/page.tsx:38` — plan view

Both render, both emit SVG, and node states come through — the P2 run shows
`PENDING, RUNNING, MERGED, ACCEPTED`, and `run-774cb…` shows `PENDING, RUNNING`, matching
its 36 pending / 9 running rows in the ledger.

**How the wrong finding was produced, because the mechanism matters more than the item.**
The DAG was requested at `/runs/maestro/<runId>/plan`. The source id is not `maestro` — it
is `maestro:lexgenius-pipeline`, URL-encoded `maestro%3Alexgenius-pipeline`. Every request
to a nonexistent source returns a banner page, and the banner is the *same size for every
run*. Two structurally different runs — 6 lanes and 45 nodes — came back byte-identical at
26,974 bytes, and that identity was the signal that the URL was wrong rather than the
component missing. It was not followed up. A corroborating `grep` for importers then
returned empty and was reported as fact without being re-run.

Working URL:

```
http://localhost:4317/runs/maestro%3Alexgenius-pipeline/run-c0523695712b495eac9b1f4b311e9d50/plan
```

**Do not open work against this item.** If the DAG needs changes, that is a fresh
observation against the rendered page, not this one.

---

## D3 — the plan name is reverse-derived and fails across repositories

**Severity: medium.** Partly an operator trap, partly a real design limit.

### Evidence

The operator trap: `plansDir` is a constructor argument defaulting to `null`
(`server/maestroDb.ts:217`), so launching the API **without** `MAESTRO_PLANS` makes
`planNameFor` return null for every run. That alone explains most null names.

The real limit, with `MAESTRO_PLANS` correctly set:

```
runs: 11  with plan_name: 9
  run-c0523695712b495e -> NULL                    9d599755fc
  run-9f76fa05879f49fb -> NULL                    9d599755fc
  run-774cb49671174be9 -> cmo-consolidation-l-r6  aa73fb5390
```

The two nulls are the P2 runs. Their plan lives in the **lexgenius** plans directory, not
lexgenius-pipeline's, and the server takes one plans directory.

### Root cause

`planNameFor` (`server/maestroDb.ts:321`) re-hashes plan files on disk and matches the
digest. That is fragile by construction: it breaks when the plan is edited (digest moves),
moved, deleted, or — as here — shipped from a different repository than the one the
console is pointed at.

### Required change

Record the plan name **in the ledger at run creation**, so it never needs deriving.

`_RUNS_ADDED_COLUMNS` already exists at `adw_modules/lifecycle.py:359` and
`_ADDED_COLUMNS` wires `runs` into the additive migration at `:398`. Add `plan_name TEXT`
there — the same additive path #128 used for `attempt_host` — and write it in the
`INSERT INTO runs` at `:915`.

Then have the console prefer the stored name and fall back to the digest lookup for old
rows, so existing ledgers keep working.

On the "prompt the user for a plan name" request: the name should come from the plan being
shipped, not from an interactive prompt at run start — a prompt cannot be answered by a
scheduled or headless run, and §1.2 keeps prose out of lifecycle records. If the plan
carries no usable name, that is worth refusing at ship time rather than papering over at
display time.

### Acceptance

- A new ledger records `plan_name` at run creation; a test asserts it round-trips.
- An **old** ledger with no `plan_name` column still opens and still projects — additive
  means additive. Test this explicitly; a migration that breaks old ledgers is worse than
  the bug.
- A run whose plan lives outside the configured `MAESTRO_PLANS` directory still shows its
  name.
- `tests/test_template_parity.py` green, and `runtime_sync.py check` reports template and
  the-library level. Mirror with `runtime_sync.py mirror --apply`, never `cp`.

---

## Constraints for whoever picks this up

- **Never `git stash`.** The stash stack is repo-global across every worktree on this
  machine; a stash steals another lane's work and reports it as yours.
- D1 and D2 are console-only (`.claude/skills/sssf/apps/`). D3 touches the runtime and
  **must** be mirrored to the-library. Do not commit in the-library without confirming
  `git status` there — parity green says the bytes agree, never that they are committed —
  and stage only `skills/sssf/templates/adws`; a `git add -A` there sweeps unrelated
  in-flight work into a commit claiming to be a mirror.
- Suite: `PYTHONPATH=. .venv/bin/python -m pytest tests -q` from `templates/adws`. System
  python3 is 3.14 and has no pytest. Do **not** pass `-o addopts=` on a full-suite run —
  it throws away `-n auto`. Pass it only when counting one selector.
- Compare failing test **ids as sorted sets**, not counts. A count that moves by the same
  number in both directions hides a swap.
- `rm` is aliased to trash and `ls` prefixes glyphs — use `/bin/rm` and `/bin/ls`.
- Do not run `run start` or `run resume`.

## Suggested order

D3's runtime column, then D1. D1 and D3 both touch the run projection, and doing D3 first
means D1 writes against the final `MaestroRunSummary` shape rather than against one that is
about to change. D2 is withdrawn — there is no third task.
