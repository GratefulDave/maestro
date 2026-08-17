# Config Reference

The full `sssf.config.yaml` spec: every field, how defaults merge, and how model / thinking / tools / extensions map onto the coding agent.

It lives at **`adws/adw_sssf_config/sssf.config.yaml`** — the default path every `adw_*.py` and the justfile resolve, and where `install.py` / `make_config.py` stamp it. Pass `--config <path>` to any ADW (or set `SSSF_CONFIG` for the justfile) to run against a different roster.

## Shape

```yaml
defaults:
  coding_agent: omp
  model: openai-codex/gpt-5.6-terra
  thinking: medium
  harness_engineering: []
  tools: [read, bash, write, grep, glob]
  data_dir: adws/adw_data

observability:
  db: adws/adw_data/sssf.db
  poll_ms: 500

agents:
  - name: planner
    coding_agent: claude_code
    model: opus
    thinking: high
    purpose: Turn a request into a plan the builder can implement without asking questions.
    prompt_engineering:
      system: adws/adw_data/prompt_engineering/planner/system.md
      user: adws/adw_data/prompt_engineering/planner/user.md
    tools: [Read, Grep, Glob, Bash, Write]

  - name: builder
    pm_profile: grok
    purpose: Implement the plan exactly; report every changed file in the envelope.
    prompt_engineering:
      system: adws/adw_data/prompt_engineering/builder/system.md
      user: adws/adw_data/prompt_engineering/builder/user.md
```

## Fields

### `defaults`

| Field | Type | Meaning |
|---|---|---|
| `coding_agent` | `omp` \| `claude_code` | Which implemented interface runs the agent. |
| `model` | string | OMP model binding when no `pm_profile` is set; direct Claude Code model name otherwise. The starter OMP default is `openai-codex/gpt-5.6-terra`. |
| `thinking` | enum | Reasoning effort — see below. Default `medium`. |
| `color` | hex string | Lane color for every agent that does not set its own. Default empty — the visualizer falls back to its own palette. |
| `harness_engineering` | list[string] | OMP extension paths. Direct Claude Code rejects extensions because it cannot enforce their capabilities. |
| `tools` | list[string] | Roster-wide tool allowlist. Every `claude_code` agent must provide one; OMP may inherit this default. |
| `protected_files` | list[string] | Paths **no** agent may modify unless it names them in its own `writes`. Default: `adws/adw_modules/`, `adws/adw_sssf_config/`, `adws/adw_*.py` — an agent must not be able to edit the machinery that decides whether its work passed. |
| `data_dir` | path | Runtime home. Sessions land at `{data_dir}/sessions/{adw_id}/{agent_name}/`. Default `adws/adw_data`. |

### `observability`

| Field | Type | Meaning |
|---|---|---|
| `db` | path | SQLite trace db. `tracer.py` writes it directly; the visualizer polls it. Default `adws/adw_data/sssf.db`. |
| `poll_ms` | int | Visualizer live-poll cadence in ms. History uses the same queries, lazy-paged. Default `500`. |

### `agents[]`

| Field | Required | Meaning |
|---|---|---|
| `name` | yes | The identifier ADW scripts use. **ADWs name agents, never models.** |
| `purpose` | yes | One sentence: what this agent is for. Should match its `system.md` Purpose. |
| `prompt_engineering.system` | yes | Path to the system prompt — who the agent is, its single purpose, its output contract. |
| `prompt_engineering.user` | yes | Path to the default user prompt — the task template with `{{prompt}}`, `{{previous_envelope}}`, `{{context_handoff_dir}}`. |
| `color` | no | Hex swatch (`"#a78bfa"`) for this agent's lane in the visualizer. Travels config → `agent_sessions.color` → `/api/sessions/:adw_id`, and rides the `agent_start` event so a lane is colored while the agent is still running. Unset = the UI's fallback palette. |
| `coding_agent`, `model`, `thinking`, `harness_engineering` | no | Override the corresponding `defaults` key. |
| `pm_profile` | no | OMP profile name. When present, OMP runs profile mode instead of explicit provider/model/thinking flags. |
| `tools` | no | Allowlist. Direct Claude Code requires it; OMP inherits defaults when omitted. A capability list, not a boundary — see `writes`. |
| `writes` | no | What this agent may modify **in the repo**, enforced after every call. Omitted = unrestricted (still barred from `protected_files`). `[]` = no repo writes at all. A list = only those paths: a trailing `/` is a directory prefix, `*` matches within one path segment, `**` crosses segments, anything else is an exact path. Naming a `protected_files` path here is what unlocks it. **The session runtime under `data_dir` is always writable** — `writes: []` means read-only with respect to the repo, not unable to write its own report. |

Output types are deliberately absent: config defines who an agent *is*; the ADW call site defines how it's *used*. One agent serves many calls — same system prompt, different user prompt + output type per call.

## Defaults merging

`agents.py` merges each entry **over** `defaults`, key by key. An entry states only what differs; anything unset inherits. `agents.validate(cfg, REQUIRED_AGENTS)` then confirms every name an ADW declares exists, resolves to a usable coding agent + model, and has both prompt files present on disk. Any miss fails the run immediately — **no agent is ever spawned against a half-valid config.**

## Thinking levels

```text
off | minimal | low | medium | high | xhigh | max
```

For OMP without `pm_profile`, the value is passed as `--thinking`. In profile mode, the selected OMP profile owns route-specific behavior. Direct Claude Code receives the same value as `--effort`.

## Model resolution

For OMP without `pm_profile`, write `model` as an explicit `provider/model-id` listed by `omp models`; ambiguous or unknown bindings fail before launch. Profile mode deliberately omits provider/model/thinking flags and records the model OMP reports as having run. Direct Claude Code receives the configured model name unchanged and records the model returned by its stream.

Provider credentials belong to the selected OMP provider or Claude CLI login, never this YAML. Changing a model or profile does **not** guarantee a fresh context: direct Claude Code scopes persistence to its matching request, while OMP resumes state from its per-agent session directory. Use a new `--adw-id` when a clean context is required.

## Tools

`tools` is passed to the selected runtime. The starter OMP roster uses lower-case OMP names such as `read`, `grep`, `glob`, `bash`, and `write`; extension-provided OMP tools must also appear in that list. Direct Claude Code requires an explicit allowlist of its builtin capabilities and refuses MCP capabilities or extensions. An empty allowlist deliberately yields a no-tools Claude run.

Tool allowlists are not repository-write boundaries. Use `writes` and `protected_files` for that boundary.

## Write permissions — `writes` and `protected_files`

`tools` cannot express a safety boundary, because two of the tools are general
purpose. `bash` runs anything, including `git checkout`, which discards an
engineer's uncommitted work; `write` reaches any path, not only the one report
file an agent was granted it for. So "this agent changes nothing" is a claim a
tool list can state but never keep.

`adw_modules/permissions.py` keeps it, the same way every other claim in this
system is kept — after the fact, against the repo. Before an agent's first
prompt the working tree's change-set is fingerprinted; after its last send
(including JSON retries and gate corrections) it is fingerprinted again. Any
path that appeared, vanished, or changed is attributed to that agent.

Comparing change-sets rather than watching writes is deliberate: a path that was
modified before the agent ran and is clean afterwards has been **reverted**, and
a reversion is a modification. That is what catches `git checkout`.

A breach is not a gate violation. Gates are for work an agent can be asked to
redo; a write has already happened, so re-prompting fixes nothing. Instead:

1. every unauthorized change the agent **introduced** is rolled back — tracked
   files with `git checkout --`, untracked files by deletion;
2. a path that was **already dirty** before the agent ran is left untouched. The
   operator had uncommitted work there, and discarding it to tidy up would be
   the same harm this module exists to prevent;
3. the phase fails and names every path with what happened to it.

```yaml
defaults:
  protected_files: [adws/adw_modules/, adws/adw_sssf_config/, "adws/adw_*.py"]

agents:
  - name: builder      # no `writes` key -> unrestricted, minus protected_files
  - name: scout
    writes: []         # no repo writes; its findings still land in context_handoff/
  - name: planner
    writes: [specs/]
  - name: documenter
    writes: [app_docs/, docs/, "**/*.md", "*.md"]
```

**The session runtime under `data_dir` is always writable, for every agent.**
`context_handoff/` is how agents hand work to each other, and each agent's
prompts, `raw_output.jsonl`, and `envelope.json` sit beside it. That grant comes
from `data_dir` rather than from `.gitignore`: the runtime is normally ignored,
so it never even appears in a snapshot, but an agent's ability to record its own
work must not depend on a gitignore line someone can delete.

Narrow by role, not by reflex. Anything that must produce a `context_handoff/` artifact needs `write`, or it will resort to a `bash` heredoc. Withhold `edit`/`write` only where the restriction *is* the guarantee — a reviewer that cannot edit cannot quietly fix what it was asked to report.

### Extension tools must be named explicitly

`omp --tools` is an allowlist over **builtin, extension, and custom tools alike**. The moment an OMP agent has a `tools` list — its own or inherited from `defaults` — any tool registered by `harness_engineering` is excluded unless it appears by name.

The extension still loads and the run may still succeed, but the model never receives its omitted tool.

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

Rule: **every OMP extension tool must also be named in that agent's `tools` list.** Direct Claude Code rejects `harness_engineering`; it has no corresponding extension path.

## Harness engineering

OMP `harness_engineering` entries are extension file paths, passed as `omp -e <path>`, one flag per entry and scoped to that agent. Use them for a harness capability such as the stamped `subagents.ts` extension. Every tool it registers must be explicitly added to `tools`.
