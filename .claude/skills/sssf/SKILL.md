---
name: sssf
description: Super Simple Software Factory — deploy and operate repeatable agents+code workflows (ADWs) in any codebase. Use when the user says /sssf install, wants to create/run/update an ADW, manage the agent roster in sssf.config.yaml, or observe running agent workflows. Keywords - sssf, software factory, ADW, AI developer workflow, agent pipeline, install factory.
argument-hint: "[install | create adw | run adw | update config | ...]"
---

# Super Simple Software Factory (SSSF)

Reusable combination of **agents plus code**: sequential ADW scripts own phase order and acceptance; Maestro is a separate stamped nine-stage artifact factory (`adws/maestro.py run start|resume|amend|status`) whose durable authority is `lane_state.stage` plus immutable artifacts. OMP and Claude Code agents work inside bounded phases; typed JSON envelopes carry sequential-ADW context; everything streams into SQLite for the polled visualizer. Agent proposes, code disposes. Herdr/OMP are transport only.

## Startup

Do not inventory the repository, scan ADW files, read runtime state, or print a dashboard at startup.

If the factory is installed, Maestro's operator surface is only `uv run adws/maestro.py run start|resume|amend|status` from that stamped `adws/` copy. It is not coordinator or workspace authority. Sequential ADWs keep their own entrypoints. Route only the engineer's explicit request through the table below. If a request requires the factory but `adws/` is absent, say that it is not installed and point to the install cookbook.

## Orchestrator rules

You run the system, observe the system, and help the user interact with it. **You do no ADW work yourself:**

- Never implement, plan, or test in an agent's place — launch the ADW and watch it.
- Never edit files inside `adws/adw_data/sessions/` — that is the run record.
- Observe by querying `adws/adw_data/sssf.db` (WAL — reads never block writers) **when observing is the task**. This is a capability, not a startup step: query it to follow a run you launched or one the engineer asked about, never to volunteer a status report nobody requested.
- Report phase status plainly: name, owner, status, error if any.

## Request routing (lazy-load the cookbook, then follow it)

| Request | Cookbook |
|---|---|
| `/sssf install`, set up the factory in this repo | [cookbooks/install.md](cookbooks/install.md) |
| create a new ADW / workflow | [cookbooks/create_adw.md](cookbooks/create_adw.md) |
| modify an existing ADW chain | [cookbooks/update_adw.md](cookbooks/update_adw.md) |
| create the config / agent roster | [cookbooks/create_config.md](cookbooks/create_config.md) |
| add or retune an agent (model, thinking, tools, prompts) | [cookbooks/update_config.md](cookbooks/update_config.md) |
| extend adw_modules with new low-level logic | [cookbooks/update_modules.md](cookbooks/update_modules.md) |
| run / monitor an ADW | [cookbooks/how_to_prompt_for_the_eng.md](cookbooks/how_to_prompt_for_the_eng.md) **first**, then [cookbooks/run_adw.md](cookbooks/run_adw.md) |
| turn a request into an ADW prompt | [cookbooks/how_to_prompt_for_the_eng.md](cookbooks/how_to_prompt_for_the_eng.md) |

Deep specs, when needed: [references/config.md](references/config.md) · [references/handoff.md](references/handoff.md) · [references/observability.md](references/observability.md)

## Hard rules (enforced across everything the factory generates)

1. **Validate before running** — every ADW declares `REQUIRED_AGENTS` and calls `agents.validate()` first; a missing/misnamed agent fails before anything spawns.
2. **Typed outputs only** — every agent call pairs with a concrete `EnvelopeBase` subclass in `adw_modules/data_types.py`; parse failures re-prompt the same session (context intact), never restart.
   **The output contract is a synced triad**: (a) the type in `data_types.py`, (b) the JSON example in the agent's `user.md` `## Report` section, (c) `output_type=` at every call site. These are ONE contract — change any one, update all three in the same edit (grep the type name to find every call site).
3. **Gates validate claims, not guesses** — `gate(envelope, run) -> list[str]` violations; failures return to the same session as corrections.
4. **Four-param rule** — any function with more than 4 parameters takes one concrete data type instead (`AgentCall`, `PhaseParams` are the pattern).
5. **One agent, one prompt, one purpose** — identity lives in `system.md`; task shape (user prompt + output type) lives at the call site.
6. **ADW scripts stay thin** — all low-level logic lives in `adw_modules/`.
7. **Every phase earns a description** — one sentence on what it does and why, never a restatement of its name. It is the only intent the trace, the console, and the UI ever show; `commit_plan: "Commit the plan"` is rejected at construction, blank is too.
8. **A known command is code, not an agent** — if you can write the invocation down (`bun test`, `ruff check`), it belongs in a `kind="code"` phase via `adw_modules/quality.py`. Agents are for the parts that need reading and deciding; failures come back to the builder as an envelope either way.
9. **`tools:` is a capability list, `writes:` is the boundary** — `bash` runs anything (including `git checkout`) and `write` reaches any path, so a tool list can never make "this agent changes nothing" true. `writes:` per agent and `protected_files` in defaults are enforced in `adw_modules/permissions.py` after every agent call: unauthorized changes are rolled back and the phase dies. The session runtime under `data_dir` is always writable — a read-only agent is read-only with respect to the REPO, never mute.
10. **Every ADW ends in `run.finish()`** — phases passing is not the same as the run being accepted. A test phase that ran a red suite succeeded at its job. Pass `accepted=` so the exit code, the session status, and the banner are decided together and cannot disagree.

## Runtime routes

`coding_agent: omp` runs non-interactive `omp -p --mode json`; an optional `pm_profile` selects OMP profile mode, otherwise the configured provider/model and thinking level are passed explicitly. `coding_agent: claude_code` runs direct non-interactive Claude Code with an explicit model, effort, and tool allowlist. Both are implemented and validated before launch.

Maestro is a separate stamped CLI: invoke it from the deployment copy as `uv run adws/maestro.py run start|resume|amend|status`. Template-source run creation refuses `RUN_REPOSITORY_MISMATCH`. It is not a shell executable named `maestro`. Durable stage is `lane_state.stage`; resume restarts the incomplete stage from the last immutable artifact. There is no retry/skip/abandon/cancel/bootstrap/plan subcommand.
