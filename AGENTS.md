<!-- Generated: 2026-08-15 | Updated: 2026-08-15 -->

# maestro

## Purpose
Maestro is a dependency-DAG software factory. Repository materials define the factory contract, prompt and skill templates, ADW runtime implementation, test fixtures, and a browser-based visualizer.

## Key Files
| File | Description |
|------|-------------|
| `README.md` | Project overview and usage guidance |
| `MAESTRO architecture.md` | Architecture and design reference |
| `maestro_prompt.md` | Primary Maestro prompt/specification |
| `FABLE prompt.md` | Related prompt and operating contract |
| `LICENSE` | Repository license |

## Subdirectories
| Directory | Purpose |
|-----------|---------|
| `.claude/` | Skills, SSSF templates, runtime scripts, and application code (see `.claude/AGENTS.md`) |
| `docs/` | Rendered documentation assets (see `docs/AGENTS.md`) |
| `images/` | Architecture and workflow diagrams (see `images/AGENTS.md`) |

## For AI Agents

### Working In This Directory
- Treat `MAESTRO architecture.md`, prompts, and README as contract-bearing docs; keep implementation and docs aligned.
- Keep generated/runtime state directories out of source changes.
- Prefer existing SSSF patterns under `.claude/skills/sssf/` before introducing new structure.

### Testing Requirements
- Python runtime changes: run the focused tests under `.claude/skills/sssf/templates/adws/tests/`.
- Visualizer changes: use its package scripts and exercise the affected UI path.
- Documentation-only changes: validate links and referenced paths.

### Common Patterns
- Dependency-DAG orchestration with explicit lifecycle and acceptance gates.
- Python implementation and tests live with the SSSF template.
- Prompt templates define agent roles and are part of the executable contract.

## Dependencies

### Internal
- `.claude/skills/sssf/` contains the implementation and templates described by the root docs.

### External
- Python tooling for ADW runtime and tests.
- Bun/Vite/Vue tooling for the visualizer.

<!-- MANUAL: Any manually added notes below this line are preserved on regeneration -->
