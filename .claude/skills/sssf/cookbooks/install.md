# Install

`/sssf install` — stamp the entire factory out of the skill and into the current working directory.

## Run it

```bash
uv run .claude/skills/sssf/scripts/install.py
```

Run from the **target repo root** — the cwd is where everything lands. If the skill lives in your user scope, the path is `~/.claude/skills/sssf/scripts/install.py`.

## What gets stamped

`install.py` copies `templates/` into the cwd:

| Stamped | From | Tracked? |
|---|---|---|
| `adws/adw_sssf_config/sssf.config.yaml` | `templates/sssf.config.yaml` | yes — the agent roster |
| `adws/maestro.py`, `adws/maestro.config.yaml` | `templates/adws/` | yes — the Maestro control-plane CLI and repository config |
| `.env.sample` | `templates/env.sample` | yes |
| `adws/adw_*.py` | `templates/adws/` | yes — starter ADW entrypoints |
| `adws/adw_modules/` | `templates/adws/adw_modules/` | yes — shared low-level logic |
| `adws/adw_data/prompt_engineering/{planner,builder,scout,reviewer,documenter}/` | `templates/prompt_engineering/` | yes — **the user-owned home for prompts** |
| `adws/adw_data/harness_engineering/` | `templates/harness_engineering/` | yes — **the user-owned home for OMP extensions** |
| `justfile` | `templates/justfile` | yes — starter recipes: `just demo`, the workflows, the trace reads, `just obs` |
| `adws/adw_data/sessions/`, `adws/adw_data/sssf.db` | created at runtime | no — gitignored |

The two `*_engineering` dirs mirror the two config keys of the same name: `prompt_engineering` is what an agent is told, `harness_engineering` is what its harness can do. Both are yours the moment they are stamped. Edit them in `adws/adw_data/`, never back inside the skill.

`harness_engineering/` ships with `subagents.ts` — the OMP extension backing `subagent_create` / `_continue` / `_list` / `_remove`, wired to the planner and scout in the starter roster.

## Idempotency

Re-running is safe. `install.py` skips **every** file that already exists — your config, your prompts, and previously stamped code alike — and reports what it skipped, so a second run doubles as a drift check. To refresh all factory files, run with `--force`; that overwrites every stamped file, including root `.env.sample`, `justfile`, `sssf.config.yaml`, and `prompt_engineering/`, so commit or back up user-owned edits first.

To refresh only the installed factory under `adws/`, while preserving root files and `.gitignore`, run:

```bash
uv run .claude/skills/sssf/scripts/install.py --force --adws-only
```

## Post-install checklist

1. **Credentials** — `cp .env.sample .env`, then set only credentials selected by your roster. OMP profile routes use their configured profile authentication; the starter Claude route needs an authenticated `claude` CLI.
2. **Binaries** — confirm `omp --version` for OMP agents and `claude --version` for any `claude_code` agent. Configure a different binary path only when the matching runtime option requires it.
3. **Roster** — keep explicit `provider/model-id` values for OMP's model route, or use a named `pm_profile`; validate the smallest ADW that names each edited agent before a larger run.
4. **Gitignore** — `install.py` appends `adws/adw_data/sessions/`, `adws/adw_data/sssf.db*`, and `.env` for you; confirm they landed. All three are runtime or secrets and must never be committed.
5. **Git repo** — ADWs that end in a commit phase call `git_helper.commit_all`, which raises if the cwd is not a git repository. Run `git init` and make a first commit before using `adw_plan_build.py`, `adw_plan_build_test.py`, or `adw_simple_sdlc.py`. `adw_document.py` needs one too: it measures the change with `git diff` against a base ref (`main` by default, `--base` to override).
6. **CLI smoke** — prove the installed Maestro surface exists without starting a run:

```bash
uv run adws/maestro.py workspace --help
```

Then, only after credentials and a roster are ready, run `just demo` or a smallest relevant ADW. A successful Maestro help command proves installation only; it does not validate a workspace manifest, receipts, routes, or agent credentials.
