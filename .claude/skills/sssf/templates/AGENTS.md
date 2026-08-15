<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-08-15 | Updated: 2026-08-15 -->

# templates

## Purpose
Installable SSSF configuration and prompt templates plus the Python ADW runtime and tests.

## Key Files
| File | Description |
|------|-------------|
| `sssf.config.yaml` | Factory configuration schema and defaults |
| `justfile` | Common template operations |
| `env.sample` | Environment variable example |

## Subdirectories
| Directory | Purpose |
|-----------|---------|
| `adws/` | ADW runtime, modules, and tests (see `adws/AGENTS.md`) |
| `prompt_engineering/` | Role-specific system/user prompts (see `prompt_engineering/AGENTS.md`) |
| `harness_engineering/` | Harness helper templates (see `harness_engineering/AGENTS.md`) |

## For AI Agents
Runtime modules are contract-sensitive. Preserve lifecycle, scheduler, workspace, and acceptance invariants. Keep prompt roles explicit.

## Testing Requirements
Use pytest focused on the changed ADW module or behavior.

<!-- MANUAL: Any manually added notes below this line are preserved on regeneration -->
