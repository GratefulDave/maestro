<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-08-15 | Updated: 2026-08-15 -->

# sssf

## Purpose
SSSF (structured software factory) skill: configuration, prompts, operational docs, scripts, templates, and the ADW runtime.

## Key Files
| File | Description |
|------|-------------|
| `SKILL.md` | SSSF skill contract |

## Subdirectories
| Directory | Purpose |
|-----------|---------|
| `templates/` | Factory templates and ADW implementation (see `templates/AGENTS.md`) |
| `scripts/` | Installation and generation scripts (see `scripts/AGENTS.md`) |
| `references/` | Detailed contracts and operational references (see `references/AGENTS.md`) |
| `cookbooks/` | Task-oriented operating procedures (see `cookbooks/AGENTS.md`) |
| `apps/` | SSSF visualizer application (see `apps/AGENTS.md`) |

## For AI Agents
Treat config schemas, prompt files, and runtime APIs as one contract. Update references/cookbooks when behavior changes.

## Testing Requirements
Run focused tests in `templates/adws/tests/`; run the visualizer's package checks for frontend changes.

<!-- MANUAL: Any manually added notes below this line are preserved on regeneration -->
