# Update Config

Add or retune agents in `sssf.config.yaml`.

## Retune model or thinking

Edit the agent's entry in place:

```yaml
  - name: reviewer
    model: openai-codex/gpt-5.6-terra  # OMP without pm_profile: provider/model-id
    thinking: high
```

OMP agents without `pm_profile` require `provider/model-id`; direct Claude Code receives its configured model name unchanged. Do not substitute routes or model identifiers by guess: `agents.validate()` resolves explicit OMP bindings before an agent starts.

Thinking values are `off | minimal | low | medium | high | xhigh | max`. OMP passes the value as `--thinking` only outside profile mode; direct Claude Code passes it as `--effort`.

**A model or profile change is not a context reset.** `agent_map.json` records each coding agent's model and logical session identity. Direct Claude Code scopes its persisted session to the matching request; OMP resumes the state it keeps under the per-agent session directory. Use a new `--adw-id` for a guaranteed isolated run.

## Recolor an agent's lane

```yaml
  - name: builder
    color: "#22d3ee"      # hex; the starter roster ships violet/cyan/amber/green
```

Purely cosmetic and safe to change mid-project: the color rides the `agent_start` event and the `agent_sessions` row, so the visualizer picks it up on the next run without touching past sessions. Omit the key to let the UI's fallback palette choose.

## Retune tools

`tools` is a route-specific allowlist. The starter OMP default is `read`, `bash`, `write`, `grep`, and `glob`; OMP has no partial-edit tool, and directory listing belongs in `bash`. Direct Claude Code requires its own explicit builtin allowlist, such as `Read`, `Grep`, `Glob`, `Bash`, and `Write`.

Set the roster-wide OMP default, then narrow per agent:

```yaml
defaults:
  tools: [read, bash, write, grep, glob]

agents:
  - name: reviewer
    tools:
      - read
      - grep
      - glob
      - bash
      - write
```

An agent's own list wins; otherwise it inherits `defaults.tools`. For direct Claude Code, an explicit list is required. An empty list deliberately starts a no-tools Claude run.

Narrow by role, not by reflex:

- Any agent that must produce a `context_handoff/` artifact needs **`write`**, or it must use a shell heredoc.
- Withhold `write` only where the restriction is the guarantee. The reviewer's contract is "change nothing"; enforce that with `writes: []`, not a prompt.
- Recon agents need only their actual search surface; the starter scout also receives the stamped subagent extension tools.

**OMP extension tools count against the allowlist.** `omp --tools` filters builtin, extension, and custom tools alike. Once an OMP agent has a `tools` list — its own or inherited — a `harness_engineering` tool is absent unless it is named there.

## Add harness extensions

`harness_engineering` is OMP-only. Each entry is an extension file path passed as `omp -e <path>`:

```yaml
  - name: scout
    harness_engineering:
      - adws/adw_data/harness_engineering/subagents.ts
    tools:
      - read
      - grep
      - glob
      - bash
      - write
      - subagent_create
      - subagent_continue
      - subagent_list
      - subagent_remove
```

Adding a tool-registering extension is a two-part edit: its path goes in `harness_engineering`, then every registered tool goes in `tools`. Direct Claude Code rejects extensions rather than silently accepting unmapped capability.

## Add a new agent

Three steps, all required — skipping any one fails `agents.validate()` at ADW startup, before anything spawns:

1. **Prompts.** Create `adws/adw_data/prompt_engineering/{name}/system.md` (Purpose + Instructions — the agent's static identity, nothing else) and `user.md` (an h3 per incoming datum: `{{prompt}}`, `{{previous_envelope}}`, `{{context_handoff_dir}}`, then the task, then a `## Report` section showing the exact output JSON). Copy an existing pair as the shape.
2. **Config entry.** Name, purpose, prompt refs, plus anything that differs from `defaults`.
3. **An output type.** Every agent call parses against a concrete Pydantic model in `adw_modules/data_types.py`. If none of `PlanOutput`, `BuildOutput`, `ScoutOutput`, `ReviewOutput`, `DocumentOutput` fits the new agent's report, add one — see `update_modules.md`. The user prompt's `Report` section must show exactly that JSON shape.

Then name the agent in an ADW's `REQUIRED_AGENTS` and call it.

## Rules that do not bend

- ADW scripts name **agents**, never models. Swapping a model is a config edit and touches no Python.
- One agent, one prompt, one purpose. If an entry needs two purposes, it is two agents.
- Output types never appear in config — they live at the call site, paired with the user prompt.

Full spec: `references/config.md`.
