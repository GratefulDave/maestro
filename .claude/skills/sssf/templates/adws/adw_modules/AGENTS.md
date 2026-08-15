<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-08-15 | Updated: 2026-08-15 -->

# adw_modules

## Purpose
Composable Python modules implementing Maestro planning, DAG scheduling, agent launch, workspace/worktree management, lifecycle, retries, verification, persistence, publication, and observability.

## Key Files
| File | Description |
|------|-------------|
| `coordinator.py` | Multi-node orchestration |
| `scheduler.py` | Dependency-aware scheduling |
| `launcher.py` | Launcher interface and execution |
| `lifecycle.py` | Node lifecycle transitions |
| `verification.py` | Acceptance and evidence checks |
| `workspace_runtime.py` | Runtime workspace control |

## For AI Agents
Preserve typed contracts and state-machine invariants. Avoid hidden retries or implicit fallback behavior. Update tests for observable changes.

## Testing Requirements
Run focused pytest tests covering the changed module and transition/error boundaries.

<!-- MANUAL: Any manually added notes below this line are preserved on regeneration -->
