# Run ADW

Run a workflow and report on it. **You run and observe — you never step into the process or do the work yourself.**

## Step 0 — translate the request

**Read [how_to_prompt_for_the_eng.md](how_to_prompt_for_the_eng.md) before you launch anything.** The prompt you pass is read by every agent in the chain, so it gets written deliberately: same intent, sharper words, verified paths, and a stated "done means". That cookbook is the whole procedure; this one starts once you have the prompt.

## The orchestrator's posture

The ADW is the worker. Your job is to launch it, watch the trace, and tell the engineer what happened. Do not read the agent's target files and "help", do not fix the code an agent was supposed to fix, do not edit an envelope. If a run fails, report the failing phase and its violations — the fix is a config, prompt, or ADW change, made deliberately, and then a re-run.

## Launch

Which chain to launch is decided in `how_to_prompt_for_the_eng.md`; use `just --list` to see the stamped entrypoints. The named ADW is authoritative. The examples below are concrete starter chains, not a substitute for its declared phase contract.

```bash
uv run adws/<end-to-end-chain>.py "add a /health endpoint"
uv run adws/<plan-build-verify-chain>.py requests/health.md
uv run adws/<build-first-chain>.py "implement the plan" --adw-id a1b2c3d4
uv run adws/<recon-chain>.py "where is auth handled" --config path/to/other.config.yaml
```

The prompt is inline text or a file path. Launch in the background so you can poll while it works; the `adw_id` is printed on startup — capture it, everything else keys off it.

### Choose the roster deliberately

The chain says *what runs*; the config says *who runs it*. The default is `adws/adw_sssf_config/sssf.config.yaml`. If the engineer names a roster, a model tier, an OMP profile, or a Claude route, identify the requested YAML and pass `--config` explicitly — never silently substitute a different roster.

```bash
uv run adws/adw_plan.py "add a /health endpoint" \
  --config adws/adw_sssf_config/sssf.config.yaml
SSSF_CONFIG=adws/adw_sssf_config/sssf.config.yaml \
  just plan "add a /health endpoint"
```

Read the selected YAML before launch. Its OMP agents either use an explicit `provider/model-id` or a `pm_profile`; direct Claude Code agents carry their own model, effort, and explicit tool allowlist. A different roster changes cost, model binding, tool authority, and potentially the result.

`--adw-id` is optional on **every** ADW. Given one, the run joins that session if it exists or creates it at that id: the same `sessions/{adw_id}/` directories, `context_handoff/`, and envelopes are reused. Continuity is route-specific: direct Claude Code resumes only a saved matching request; OMP resumes the state OMP keeps in that per-agent session directory. A changed model or profile is **not** a context reset—use a new `--adw-id` when isolation is required.

## Observe

The trace db is `adws/adw_data/sssf.db`. It is WAL, so reads never block the running writers — poll it as often as you like.

```bash
# where the run stands
sqlite3 adws/adw_data/sssf.db \
  "select seq, name, kind, owner, status, attempt from phases where adw_id='a1b2c3d4' order by seq;"

# the live tail — cursor on rowid, same query the visualizer polls
sqlite3 adws/adw_data/sssf.db \
  "select rowid, type, name, started_at from events where adw_id='a1b2c3d4' and rowid > 0 order by rowid limit 50;"

# why a phase failed
sqlite3 adws/adw_data/sssf.db \
  "select attempt, gate, passed, checks_json from gate_results where adw_id='a1b2c3d4';"

# session-level status
sqlite3 adws/adw_data/sssf.db \
  "select adw_id, request, status, total_tokens from sessions order by started_at desc limit 5;"

# what an agent actually did, slowest tool calls first
sqlite3 adws/adw_data/sssf.db \
  "select name, tokens, started_at, ended_at from events
   where adw_id='a1b2c3d4' and type='tool_call' order by ended_at desc limit 20;"
```

Poll on a cursor: keep the highest `rowid` you have seen and query `where rowid > ?`. Don't re-read the whole table each pass.

`tool_call` rows carry a real span, so durations come off the columns — see `references/observability.md` for which fields each event type populates.

The ADW also narrates to stdout, and every line it prints is written to the db as a `log` event — terminal and swim lane tell the same story by construction, so tailing the background process is a valid second view rather than a competing source of truth.

Files are the raw record if you need more than the db shows: `adws/adw_data/sessions/{adw_id}/{agent}/raw_output.jsonl` (full coding-agent stream), `envelope.json` (the parsed final response), `prompts/` (exactly what was sent), and `context_handoff/` (what agents wrote for each other).

## When a run is stuck

A hung coding agent produces no events at all, so the trace goes quiet rather than red. Read it in this order:

```bash
just phases <adw_id>     # which phase is still `running`
just procs <adw_id>      # recorded live child processes, including PIDs
```

`processes` rows with `ended_at IS NULL` are the live ones. If a recorded coding-agent child has produced no `tool_call` events and its `raw_output.jsonl` is empty, launch likely failed before the first tool call: check the selected roster's route and authentication before waiting longer. To stop a stuck run, terminate the recorded child process deliberately, then record the outcome; the stamped justfile provides inspection recipes, not a `just kill` command.

After termination, inspect the run's terminal status before reporting it. Never leave an unverified process row represented as active work.

## Report

Tell the engineer, in order: which chain and which roster you launched (name the config whenever it was not the default), which phase is running now (or which failed), phase statuses in sequence, and for a failure the gate violations or the error verbatim. Remember **every phase defaults to `fail`** — a phase showing `fail` may simply never have completed; `queued` means it never started. Don't dress up a partial run as a success.

For a visual live view, the visualizer app in the skill (`just obs`, or tmux sessions viz-api :4600 + viz-ui :4601) polls this same db — sessions as cards, runs as swim lanes, phases and tool calls drill-in. The sqlite queries above remain the headless equivalent.
