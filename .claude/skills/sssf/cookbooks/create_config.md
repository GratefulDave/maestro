# Create Config

Generate `sssf.config.yaml` — the agent roster for a target repo.

## Generate it

```bash
uv run .claude/skills/sssf/scripts/make_config.py
```

Writes `adws/adw_sssf_config/sssf.config.yaml` — creating the directory if needed — with the starter agents (planner, builder, scout, reviewer, documenter) wired to the prompt files `/sssf install` stamped into `adws/adw_data/prompt_engineering/`. That path is the default every ADW and the justfile look for; `--config` overrides it. `make_config.py` refuses to overwrite an existing config unless you pass `--force`, so retuning an existing roster is a hand edit — see `update_config.md`.

## The rule

**One agent, one prompt, one purpose.** An entry defines who an agent *is*: its coding agent, model, thinking level, and exactly one system prompt plus one user prompt. How it gets *used* — the output type, a per-call user prompt override — lives at the ADW call site, never here.

## Schema

```yaml
defaults:
  coding_agent: omp
  model: openai-codex/gpt-5.6-terra  # OMP: explicit provider/model-id
  thinking: medium
  harness_engineering: []            # OMP extension paths
  data_dir: adws/adw_data

observability:
  db: adws/adw_data/sssf.db
  poll_ms: 500

agents:
  - name: planner                    # ADW scripts name agents, never models
    coding_agent: claude_code
    model: opus
    thinking: high
    dangerously_skip_permissions: true
    color: "#a78bfa"
    purpose: Turn a request into a plan the builder can implement without asking questions.
    prompt_engineering:
      system: adws/adw_data/prompt_engineering/planner/system.md
      user: adws/adw_data/prompt_engineering/planner/user.md
    tools: [Read, Grep, Glob, Bash, Write]

  - name: builder
    pm_profile: grok                 # OMP profile mode; model falls back only when no profile exists
    purpose: Implement the plan exactly; report every changed file in the envelope.
    prompt_engineering:
      system: adws/adw_data/prompt_engineering/builder/system.md
      user: adws/adw_data/prompt_engineering/builder/user.md
    tools: [read, grep, glob, bash, write]
```

Every agent entry merges over `defaults`, so an entry only states what differs. OMP tool names are lower-case; Claude's direct route uses its documented builtin names. An explicit `tools` list is required for `claude_code`; a missing OMP list inherits defaults, and `pm_profile` selects OMP's profile route instead of an explicit provider/model invocation.

## After generating

1. Each agent needs its prompt pair to exist on disk: `adws/adw_data/prompt_engineering/{name}/system.md` and `user.md`. `agents.validate()` fails the run at startup if either is missing.
2. Write `purpose` as one sentence and make the system prompt say the same thing — the two should not drift.
3. Validate by running the smallest ADW that names your agents; a bad entry fails fast, before anything spawns.

Full field-by-field spec, thinking-level mapping, and model resolution: `references/config.md`. Retuning an existing roster: `update_config.md`.
