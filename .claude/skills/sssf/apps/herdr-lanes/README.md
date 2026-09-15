# maestro-lanes

A Herdr 0.9.0 plugin for the sidebar. It shows each agent's vendor logo and a held activity
mark. On Maestro lane panes and lane Spaces it also shows the lane's stage, review round and
latest verdict.

It only displays things. It does not create, close, rename or move panes, tabs or Spaces, and
it never writes to the Maestro ledger.

## What it writes

Every write uses `--source maestro-lanes` and only these token names:

| Target | Token | Value |
|---|---|---|
| any agent pane | `logo` | vendor glyph from the Herdr agent id (claude, codex, omp, pi, grok, ...), in the Private Use Area of *Herdr Agent Icons Max*. Unset for unknown agents. |
| any agent pane | `mark` | braille spinner frame while `working`, `✓` after work finishes (held until the pane is focused), `?` while `blocked` (held until it works again) |
| lane pane (`kind=lane`, `lane`, `run_id` tokens) | `stage`, `round`, `verdict` | `lane_state.stage`, `r<N>` = number of `TEST_REVIEW` + `CODE_REVIEW` artifacts for the lane in that run, `verdict` of the newest one |
| lane Space (workspace tokens `lane`, `run_id`) | `stage`, `round`, `verdict` | same values. Older runs keep lane tabs in one shared Space, so no Space gets these tokens. |

Maestro's own tokens (`kind, lane, role, run_id, parent, repo, scratch`) and pane labels are
read, never written. Tokens carry a 180 s TTL and are refreshed every 60 s, so they disappear
on their own if the daemon dies. `stop` clears them straight away.

The ledger is found at `~/.local/state/maestro-artifact-factory/*/lifecycle.sqlite3`, or at
the colon-separated paths in `MAESTRO_LANES_LEDGERS`. Each poll (every 2 s) opens the file
with `SQLITE_OPEN_READONLY` and busy timeout 0, runs one SELECT and closes it. If the database
is busy, that tick is skipped. From a review payload the query reads only the typed `verdict`
field, through `json_extract`.

Other files it changes, each backed up once to `<file>.bak-maestro-lanes` before the first
edit:

- `~/.config/herdr/config.toml`: `configure` swaps the `[ui.sidebar.agents]` and
  `[ui.sidebar.spaces]` tables for one block between `# >>> maestro-lanes sidebar` and
  `# <<< maestro-lanes sidebar`. The old tables are saved in the plugin state directory, and
  `unconfigure` puts them back. If `herdr config check` fails, the original file is restored.
- `~/.config/ghostty/config`: `install-font` adds a `font-codepoint-map` block fenced by
  `# >>> maestro-lanes font`.
- `~/Library/Fonts/HerdrAgentIconsMax-Regular.ttf`.

## Install

```sh
herdr plugin link /path/to/.claude/skills/sssf/apps/herdr-lanes
herdr plugin action invoke maestro-lanes.install-font   # then reopen Ghostty
herdr plugin action invoke maestro-lanes.configure      # writes the block and reloads config
herdr plugin action invoke maestro-lanes.start          # startup hooks do not run on link
```

After this, Herdr's startup hook starts the daemon on every server start. A
`pane.agent_detected` hook restarts it if it has died. Commands go through `bin/run.sh`,
which finds a `node` that has `node:sqlite` (Node 22.5 or newer). This is needed because
Herdr's launchd server may not have `node` on its `PATH`. To choose a specific binary, set
`MAESTRO_LANES_NODE`. The plugin has no npm dependencies.

The daemon log is `~/.local/state/herdr/plugins/maestro-lanes/daemon.log`.

## Uninstall

```sh
herdr plugin action invoke maestro-lanes.stop            # stop and clear own tokens
herdr plugin action invoke maestro-lanes.unconfigure     # restore previous sidebar tables
herdr plugin action invoke maestro-lanes.uninstall-font  # remove font + Ghostty block
herdr plugin unlink maestro-lanes
```

## Limits

- Herdr 0.9.0 only lets a client subscribe to `pane.agent_status_changed` for a given
  `pane_id`, so status changes are picked up by the 2 s poll. Pane, Space and focus events
  still wake the daemon immediately.
- Each pane accepts sequenced token reports from at most 32 sources in its lifetime. This
  plugin uses one.

## Attribution

The icon font (`fonts/HerdrAgentIconsMax-Regular.ttf`), its codepoint map, and the socket,
subscription and font-install approach come from
[herdr-radar](https://github.com/hhdebb/herdr-radar) (MIT), which forks
qintmb/herdr-icon-agent-ui. See `LICENSE-herdr-radar` and `THIRD_PARTY_NOTICES.md`. The vendor
marks identify third-party products and do not imply affiliation.
