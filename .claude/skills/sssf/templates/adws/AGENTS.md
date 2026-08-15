<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-08-15 | Updated: 2026-08-15 -->

# adws

## Purpose
Python implementation of the Agentic Development Workflow: planning, scheduling, worktrees, launchers, lifecycle, verification, finalization, publication, and persistence.

## Key Files
| File | Description |
|------|-------------|
| `maestro.py` | Main factory/coordinator entrypoint |
| `adw_test.py` | ADW test/support entrypoint |
| `adw_modules/coordinator.py` | Dependency-DAG coordination |
| `adw_modules/scheduler.py` | Runnable-node scheduling |
| `adw_modules/launcher.py` | Agent/process launch contract |
| `adw_modules/workspace_runtime.py` | Workspace execution runtime |

## Subdirectories
| Directory | Purpose |
|-----------|---------|
| `adw_modules/` | Runtime modules (see `adw_modules/AGENTS.md`) |
| `tests/` | Focused behavioral tests and fixtures (see `tests/AGENTS.md`) |

## For AI Agents
Maintain explicit state transitions, idempotent recovery, dependency ordering, and evidence-backed acceptance. Update callers/tests with API changes.

## Testing Requirements
Run the narrowest relevant pytest test, then the affected module test set.

<!-- MANUAL: Any manually added notes below this line are preserved on regeneration -->
