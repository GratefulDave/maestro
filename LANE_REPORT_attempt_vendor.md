# Lane report — attempt vendor

Branch: `fix/attempt-vendor-write`
Worktree: `/Users/davidandrews/PycharmProjects/maestro-attempt-vendor`

## Done

Recorded the launcher's configured vendor, model, and route on the attempt
`extra_json` at dispatch, through the existing `mark_launched(..., extra=)`
seam. Did not change `mark_launched`'s signature. Did not write
`lifecycle.py`, `scheduler_types.py`, `watchdog.py`, or `salvage.py`.

New module: `.claude/skills/sssf/templates/adws/adw_modules/attempt_identity.py`

- Keys: `VENDOR_KEY`, `MODEL_KEY`, `ROUTE_KEY` — one definition each.
- `launch_identity_extra` writes only fields the launcher actually holds.
- `identity_from_record` returns `Optional[str]` per field. Missing, blank,
  and non-string are all `None` ("not recorded"). Never `""`, never a default
  vendor.
- `display` renders `None` as `not recorded`.

Writer: `maestro._launch_attempt_extra` adds those keys to the existing
`{watchdog.SESSION_PATH_KEY: transcript}` mapping. It does not replace it.

Reader (B15): `run status` via `maestro._run_progress` and
`maestro._render_progress`. JSON carries `null` vs a string. Human view always
prints `vendor:` / `model:` / `route:` so absence is visible as
`not recorded`.

`adw_modules/scheduler.py` unchanged.

## §1.2 — source at each `mark_launched` site

Current line numbers in this worktree (brief's 1301/1353/3520 have drifted).

### 1. `scheduler.py:1431` (`on_launch`)

Sees: `pid` only.

Does not see vendor, model, or route. `on_launch` is
`Callable[[Optional[int]], None]`. The scheduler has no `LaunchSpec`, no
`args.execution_vendor` / `tester_vendor`, no `args.agent_model` /
`agent_route`. `NodeExecution` has no identity fields.

Recorded: nothing.

### 2. `scheduler.py:1483` (`execution.launched_pid`)

Sees: `execution.launched_pid` only.

Same gap. `NodeExecution` is pid / exit / envelope / launcher_failure.
No vendor, model, or route.

Recorded: nothing.

### 3. `maestro.py:3664` (was brief 3520)

Sees, from the launcher's own config already bound onto `args`:

| Field | AGENT node | TESTS node |
|---|---|---|
| vendor | `args.execution_vendor` | `args.tester_vendor` |
| model | `args.agent_model` (`lane_model`) | `args.tester_model` or `args.agent_model` |
| route | `args.agent_route` (`lane_route`) | `args.tester_route` or `args.agent_route` |

`LaunchSpec` carries `model` and `route` and has no vendor field. Vendor is
the config string already loaded at `maestro.py:1203-1210`
(`execution.vendor`, `tester.vendor`). Cross-vendor check is
`code_review.require_distinct_vendor` over those strings, not a set in
`permissions.py`.

CODE nodes never reach this site. They only call `on_launch(pid)` — site 1.

A missing config vendor omits the key. Absence is correct; a guess is not.

Site 3 writes first. Site 1 then rearms pid with no `extra`.
`mark_launched` merges; `session_path` and identity survive. Covered by
`tests/test_lifecycle.py` (existing rearm) and
`tests/test_attempt_vendor.py::test_mark_launched_keeps_session_path_across_pid_rearm`.

## Tests

`tests/test_attempt_vendor.py` — 6 collected
(`-o addopts= --collect-only`):

- write: `_launch_attempt_extra` keeps `session_path` and records
  vendor/model/route
- write: `mark_launched` then pid-only rearm still holds both
- reader: absent / present / partial through `_run_progress` and
  `_render_progress`
- preexisting `AttemptRecord` → all three `None` / `not recorded`
- empty string is not recorded

Targeted: `6 passed, 3 subtests passed in 3.75s`

## Evidence — full suite

Command (both times):

```
cd .claude/skills/sssf/templates/adws
PYTHONPATH=. /Users/davidandrews/PycharmProjects/maestro/.venv/bin/python -m pytest tests -q
```

Before (dispatcher, `main` @ `fb11fd0`):
`2517 passed, 9 failed, 222 subtests passed` in 34:02.
Six failures: `tests/test_deployment_parity.py` (lexgenius +
lexgenius-pipeline). Three others were CPU-contention flakes, green on
re-run.

After (this worktree, this change):
`8 failed, 2524 passed, 7 warnings, 225 subtests passed in 1055.67s (0:17:35)`

Accounting: +6 new tests (2526 collected → 2532). The three flakes passed.
Two `tests/test_template_parity.py` cases went red because this checkout now
has `attempt_identity.py` and `test_attempt_vendor.py` and the-library does
not. Six remaining failures are the known deployment-parity drift. No other
failures.

Did not mirror into the-library: this lane's write set is the four owned
paths in this worktree. `runtime_sync.py mirror` would also refuse —
the-library is already ahead on `lifecycle.py`, `salvage.py`,
`scheduler_types.py`, `watchdog.py`.

## Not done / not touched

- `git stash`: not run
- `maestro.config.yaml`: not edited
- `.claude/skills/sssf/apps/`: not touched
- `/Users/davidandrews/PycharmProjects/lg-pipeline-fix-r7`: not touched
- No maestro run started, resumed, or cancelled
- `mark_launched` signature unchanged
