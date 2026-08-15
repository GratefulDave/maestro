<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-08-15 | Updated: 2026-08-15 -->

# tests

## Purpose
Behavioral and contract regression tests for ADW modules, numbered SSSF steps, lifecycle, scheduling, workspaces, verification, retries, and publication.

## Subdirectories
| Directory | Purpose |
|-----------|---------|
| `fixtures/` | Controlled valid/invalid inputs and enforcement fixtures (see `fixtures/AGENTS.md`) |

## For AI Agents
Test observable contracts, transitions, boundaries, and failure recovery. Keep tests deterministic and isolated.

## Testing Requirements
Run focused pytest selectors first; avoid broad suite changes unless the contract spans modules.

<!-- MANUAL: Any manually added notes below this line are preserved on regeneration -->
