# Third-party notices

herdr-lanes is an original plugin, not a fork. It ships one third-party asset, and parts of its
code are adapted from another project.

## Herdr Agent Icons Max font

`fonts/HerdrAgentIconsMax-Regular.ttf` is copied unmodified from
[hhdebb/herdr-radar](https://github.com/hhdebb/herdr-radar) (`dist/`). herdr-radar is MIT licensed
(`LICENSE-herdr-radar`, © 2025 qintmb, © 2026 herdr-kit contributors) and inherits the font and glyph
design from [qintmb/herdr-icon-agent-ui](https://github.com/qintmb/herdr-icon-agent-ui) (MIT). The font
holds vendor marks at U+E1A0–U+E1B6 and state marks at U+E1C0–U+E1C5. This plugin uses the vendor
marks only.

herdr-radar records these sources for the marks. The marks identify third-party products and do not
imply affiliation or endorsement. The open-source licences cover each project's artwork packaging,
not the trademarks.

| Mark | Source (as recorded by herdr-radar) | Licence |
|---|---|---|
| claude | Anthropic `cwc-workshops` | Apache-2.0 (`LICENSE-Apache-2.0.txt`) |
| codex | OpenAI `codex` | Apache-2.0 (`LICENSE-Apache-2.0.txt`) |
| kimi | Moonshot AI Kimi Code CLI | Apache-2.0 (`LICENSE-Apache-2.0.txt`) |
| kilo | Kilo Code CLI | Apache-2.0 (`LICENSE-Apache-2.0.txt`) |
| opencode | anomalyco `opencode` (via lobehub/lobe-icons) | MIT |
| omp | can1357 `oh-my-pi` | MIT |
| cline | Cline | MIT |
| mastracode | MastraCode | MIT |
| maki | Maki | MIT |
| pi | Pi coding agent | not recorded upstream |
| hermes | Hermes Agent (via lobehub/lobe-icons, MIT) | not recorded upstream |
| cursor, copilot, deepseek, gemini, gpt, qwen, agy, kiro | vendor marks via [lobehub/lobe-icons](https://github.com/lobehub/lobe-icons) (MIT packaging) | proprietary trademarks |
| grok | xAI Grok | proprietary trademark |
| amp, devin, qodercli | added in herdr-radar; source not recorded upstream | proprietary trademarks |

## Code

The socket client and event subscription (`lib/herdr.js`) and the approach to installing the font
and mapping its codepoints (`lib/terminal.js`) are adapted from herdr-radar `lib/ipc.js`,
`lib/subscribe.js` and `lib/font.js`, under the MIT licence in `LICENSE-herdr-radar`.

The vendor colours in `lib/brands.js` (the published hues and the dark-panel ink for monochrome
marks) and its codepoint order are taken from herdr-radar `lib/palette.js` and
`tools/codepoints.toml`, under the same MIT licence.
