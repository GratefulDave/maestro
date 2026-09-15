# herdr-lanes

A Herdr 0.9.0 plugin for macOS that improves the sidebar. Every agent pane gets a vendor logo, a held
activity mark, a name and a short title. Panes and Spaces that belong to a lane also show the lane's
stage, review round and latest verdict, read through a lane adapter.

It only changes what the sidebar displays. It never creates, closes, renames or moves panes, tabs or
Spaces, and it never writes to a lane factory's state.

## Layout

The same two agent rows are used for every pane. A token that has no value is hidden.

| | lane pane | any other agent pane |
|---|---|---|
| row 1 | `state_icon  $logo  $name` (lane id) `$role` | `state_icon  $logo  $name` (workspace label) |
| row 2 | `$mark  state_text  $stage  $round  $verdict` | `$mark  state_text  $title` (terminal title, max 40 chars) |

Space rows show `state_icon  workspace  state_text  $stage`, then `branch git_status`, then `$usage`.

`$name` is drawn in the foreground colour rather than dimmed. `$logo` is coloured by vendor
(`lib/brands.js`): a vendor with a published hue carries it, and a vendor whose mark is monochrome is
drawn in white. Herdr allows at most 16 rules per token, so 16 vendors are coloured (claude, grok,
kimi, omp, pi, codex, gpt, gemini, opencode, cursor, copilot, deepseek, qwen, cline, kilo, amp); the
others (mastracode, maki, hermes, agy, kiro, devin, qodercli) keep the default colour.

## What it writes

Every write uses metadata source `lanes`, sends a monotonic `seq`, and uses only these token names.

| Target | Tokens |
|---|---|
| agent pane | `logo` (glyph from Herdr Agent Icons Max, chosen by agent id), `mark` (braille spinner while working; `✓` held until the pane is focused; `?` held until work resumes), `name`, `title` |
| lane pane | `logo`, `mark`, `name`, plus `stage`, `round`, `verdict` |
| lane Space | `stage`, `round`, `verdict` |

Tokens that other tools write are only read, never written. Tokens expire after 180 s and the daemon
re-sends them every 60 s, so they disappear on their own if the daemon dies. `stop` clears them
straight away.

## Adapters

The daemon itself knows nothing about any particular lane factory. `lib/adapters/maestro.js` holds
everything specific to Maestro: how lane panes and Spaces are recognised from their tokens, where the
ledger lives, how rounds and verdicts are read, and the stage and role colour rules. The interface is
documented at the top of that file.

The Maestro adapter opens each ledger read-only with busy timeout 0 and runs one SELECT per 2 s poll.
If the ledger is busy, that tick is skipped. From review payloads it reads only the typed `verdict`
field.

## Files it changes

Each change is backed up once, to `<file>.bak-herdr-lanes`, before the first edit. Each change is
recorded in the plugin state directory.

- `~/.config/herdr/config.toml`: `configure` removes the hand-written `[ui.sidebar.agents]` and
  `[ui.sidebar.spaces]` tables and puts one block in their place, between `# >>> herdr-lanes sidebar`
  and `# <<< herdr-lanes sidebar`. The removed tables are saved in the state directory only after
  `herdr config check` passes. If the check fails, the original file is written back.
  `unconfigure` restores the saved tables.
- The icon font goes to `~/Library/Fonts/HerdrAgentIconsMax-Regular.ttf`.
- The terminal that hosts the Herdr client is detected from the process tree. You can override this
  with `HERDR_LANES_TERMINAL=wezterm` or `ghostty`.
  - **WezTerm:** `config.font = wezterm.font("X")` becomes
    `wezterm.font_with_fallback({ "X", "Herdr Agent Icons Max" })`, marked with a
    `-- herdr-lanes:` comment. Your font stays first so it keeps the cell metrics; the icon font only
    supplies the codepoints it lacks. A line wrapped by 0.1.0 (icon font first) is reordered in place.
    Any other form of that line is refused, and the line to add is printed.
  - **Ghostty:** a fenced `font-codepoint-map` block is added.
- `uninstall-font` reverses only what it recorded: the original WezTerm line is restored exactly, and
  the font file is removed only if its sha256 still matches the copy it installed.

## Install

```sh
herdr plugin link /path/to/.claude/skills/sssf/apps/herdr-lanes
herdr plugin action invoke herdr-lanes.configure      # sidebar block + reload
herdr plugin action invoke herdr-lanes.install-font   # then restart the terminal
herdr plugin action invoke herdr-lanes.start          # startup hooks do not run on link
```

After this, Herdr's startup hook starts the daemon whenever the server starts. A
`pane.agent_detected` hook restarts it if it has died. The daemon holds an exclusive lock
(`daemon.lock`). A lock is trusted only if its pid is alive and that process's command line is this
daemon; any other lock is treated as stale and its pid is never signalled. `bin/run.sh` finds a
`node` that has `node:sqlite`, because Herdr's launchd server may not have `node` on its PATH. Set
`HERDR_LANES_NODE` to choose a binary.

The plugin was first published as `maestro-lanes`. `configure` and `start` migrate from that name
once: they stop the old daemon, clear tokens written under source `maestro-lanes`, replace the old
fenced blocks, and carry over the saved tables and backups.

Tests: `node --test tests/lanes.test.js`

## Uninstall

```sh
herdr plugin action invoke herdr-lanes.stop
herdr plugin action invoke herdr-lanes.unconfigure
herdr plugin action invoke herdr-lanes.uninstall-font
herdr plugin unlink herdr-lanes
```

## Limits

- Herdr 0.9.0 only lets a subscription to `pane.agent_status_changed` name a single pane, so status
  changes are picked up by the 2 s poll. Pane, Space and focus events wake the daemon immediately.
- WezTerm takes cell metrics from the first font in a fallback list. If cells look different after
  the change, list your own font first.

## Attribution

See `THIRD_PARTY_NOTICES.md`, `LICENSE-herdr-radar` (MIT) and `LICENSE-Apache-2.0.txt`.
