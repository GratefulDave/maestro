# Lane report — attempt liveness identity

Branch: `fix/attempt-liveness-identity`
Worktree: `/Users/davidandrews/PycharmProjects/maestro-attempt-liveness`

## What landed

`attempts` now carries `attempt_host TEXT` and `attempt_start_epoch REAL` (nullable, no default). Additive migration via `_ATTEMPTS_ADDED_COLUMNS` registered in `_ADDED_COLUMNS`. `mark_launched` writes both only when a pid is present (`scheduler_host()` + `wd.process_start_epoch`).

`AttemptRecord` gained the two `Optional` fields. All three projections that build it select them: `LifecycleStore.get_attempt`, `LifecycleStore.attempts_for`, and `LifecycleReader.attempts` (the last is column-tolerant for a read-only unmigrated ledger).

`attempt_liveness(attempt, *, is_alive, start_epoch, host) -> Optional[bool]` lives in `lifecycle.py`. Three answers:

- `True` — pid + host + start epoch recorded, host matches (short-label, case-insensitive, same `_same_scheduler_host` as the scheduler side), live start epoch equals recorded.
- `False` — same identity recorded and host matches, process absent. Only this answer may stall or let salvage proceed.
- `None` — no pid; no host or no start epoch (pre-migration row); foreign host (process table not consulted); live start epoch missing or unequal (reuse).

Readers:

- Watchdog fails open: stalls `PROCESS_DEAD` only when `attempt_liveness is False`.
- Salvage fails closed: `True` → `SALVAGE_ATTEMPT_LIVE`; `None` → `SALVAGE_ATTEMPT_LIVENESS_UNKNOWN`; `False` → proceed.

## Files

Owned production + new tests:

- `.claude/skills/sssf/templates/adws/adw_modules/lifecycle.py`
- `.claude/skills/sssf/templates/adws/adw_modules/scheduler_types.py`
- `.claude/skills/sssf/templates/adws/adw_modules/watchdog.py`
- `.claude/skills/sssf/templates/adws/adw_modules/salvage.py`
- `.claude/skills/sssf/templates/adws/tests/test_attempt_liveness.py` (new, 22 tests)

Existing fixtures that wrote a pid without identity (they encoded the bug). Updated so they still drive the production readers under the new contract:

- `tests/test_watchdog.py`
- `tests/test_attempt_salvage.py`
- `tests/test_agent_liveness_pid.py`
- `tests/test_quiesce_liveness_completion.py`
- `tests/test_salvage_path_defaults.py`

Mirrored the same bytes into the-library's template checkout with `tools/runtime_sync.py mirror … --apply` (working tree only, no commit there) so `test_template_parity` stays green. Did not touch `scheduler.py`, `maestro.py`, `maestro.config.yaml`, `.claude/skills/sssf/apps/`, or `/Users/davidandrews/PycharmProjects/lg-pipeline-fix-r7`. Did not run `git stash`.

## Evidence

Command:

```
cd .claude/skills/sssf/templates/adws
PYTHONPATH=. /Users/davidandrews/PycharmProjects/maestro/.venv/bin/python -m pytest tests -q
```

Baseline on main @ `fb11fd0` (dispatcher-measured, not re-run here): **2517 passed, 9 failed, 222 subtests passed**. Six of those nine are `tests/test_deployment_parity.py` (lexgenius + lexgenius-pipeline). The other three were CPU-contention flakes that passed when those files were re-run alone (79 passed).

Collect-only (`-o addopts=`): **2526** before this change, **2548** after. New tests: **22** (`tests/test_attempt_liveness.py`, all passed in isolation).

After this change, same full-suite command: **2542 passed, 6 failed, 7 warnings, 222 subtests passed** in 621.54s.

The six failures are exactly the known deployment-parity cases:

- `DeploymentParity_lexgenius_pipelineTests::test_no_runtime_file_is_absent_from_either_copy`
- `DeploymentParity_lexgenius_pipelineTests::test_the_deployment_is_not_ahead_of_the_template`
- `DeploymentParity_lexgenius_pipelineTests::test_the_deployment_is_not_behind_the_template`
- `DeploymentParity_lexgeniusTests::test_no_runtime_file_is_absent_from_either_copy`
- `DeploymentParity_lexgeniusTests::test_the_deployment_is_not_ahead_of_the_template`
- `DeploymentParity_lexgeniusTests::test_the_deployment_is_not_behind_the_template`

No other failures.

## Other lane

`scheduler.py` / `maestro.py` do not need a call-site change: `mark_launched` records identity itself, and Watchdog defaults (`start_epoch=process_start_epoch`, `host=None` → `scheduler_host()`) keep the production wire intact.
