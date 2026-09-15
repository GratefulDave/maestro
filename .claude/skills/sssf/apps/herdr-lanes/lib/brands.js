'use strict';

// Vendor marks and their colours. Neutral: nothing here knows about a lane factory.
//
// Codepoints: Herdr agent id -> Private Use Area glyph in Herdr Agent Icons Max, U+E1A0
// upward, in the order of herdr-radar tools/codepoints.toml.
// Colours: herdr-radar lib/palette.js (MIT). A vendor with a published hue carries it
// (`brand`); a vendor that signs its mark in black/white carries radar's dark-panel ink.

const LOGO_ORDER = ('claude codex opencode omp cline mastracode kimi kilo maki pi hermes cursor copilot deepseek '
  + 'gemini gpt qwen grok agy kiro amp devin qodercli').split(' ');
const logoFor = (agent) => (LOGO_ORDER.includes(agent) ? String.fromCodePoint(0xe1a0 + LOGO_ORDER.indexOf(agent)) : null);

const INK = '#e9e9f0'; // herdr-radar inks.dark
const HUE = {
  claude: '#d97757', gemini: '#4285f4', kimi: '#1783ff', deepseek: '#4d6bfe',
  qwen: '#615ced', kiro: '#9046ff', cline: '#586876', kilo: '#9a9808',
};

// Herdr allows at most 16 rules per token, and there are 23 marks. These 16 are
// coloured; the rest (mastracode, maki, hermes, agy, kiro, devin, qodercli) keep
// the sidebar's default colour.
const COLOURED = ['claude', 'grok', 'kimi', 'omp', 'pi', 'codex', 'gpt', 'gemini',
  'opencode', 'cursor', 'copilot', 'deepseek', 'qwen', 'cline', 'kilo', 'amp'];

const colourFor = (agent) => HUE[agent] ?? INK;

// [[glyph, '#rrggbb']] for the sidebar builder's `styled`.
const logoRules = () => COLOURED.map((agent) => [logoFor(agent), colourFor(agent)]);

module.exports = { LOGO_ORDER, COLOURED, logoFor, colourFor, logoRules };
