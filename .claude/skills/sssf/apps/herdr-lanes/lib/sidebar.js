'use strict';

// The managed sidebar block in Herdr's config.toml.
//
// configure: drop any managed block (current or legacy fence), cut the hand-written
// [ui.sidebar.agents] / [ui.sidebar.spaces] tables, append one fenced block, and run
// `herdr config check`. On failure the original bytes are written back and nothing else
// is left behind; only on success are the backup and the cut tables saved.

const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const { configDir } = require('./herdr');
const { logoRules } = require('./brands');

const FENCE = { start: '# >>> herdr-lanes sidebar', end: '# <<< herdr-lanes sidebar' };
const SAVED = 'replaced-sidebar-tables.toml';
const TABLES = ['[ui.sidebar.agents]', '[ui.sidebar.spaces]'];
const herdrBin = () => process.env.HERDR_BIN_PATH ?? 'herdr';
const configFile = () => path.join(configDir(), 'config.toml');
const esc = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

// Remove a fenced block and collapse blank lines only where it was spliced out.
function dropFence(text, fence) {
  const re = new RegExp(`(\\n*)${esc(fence.start)}\\n[\\s\\S]*?${esc(fence.end)}\\n?(\\n*)`);
  return text.replace(re, (m, before, after, at, all) => (at + m.length === all.length ? (before ? '\n' : '')
    : '\n'.repeat(Math.min(2, before.length + after.length))));
}

function appendBlock(text, body) {
  const base = text.replace(/\n+$/, '');
  return `${base}${base ? '\n\n' : ''}${body}\n`;
}

// A table ends at the next column-0 TOML table header, never at an indented or
// array-element line that happens to start with '['.
const HEADER = /^\[\[?[A-Za-z0-9_.-]+\]\]?\s*(#.*)?$/;

function cutTable(text, header) {
  const lines = text.split('\n');
  const at = lines.findIndex((l) => l.trim() === header && l.startsWith('['));
  if (at < 0) return { text, cut: '' };
  let end = at + 1;
  while (end < lines.length && !HEADER.test(lines[end])) end += 1;
  const cut = lines.slice(at, end).join('\n').replace(/\n+$/, '');
  const rest = [...lines.slice(0, at), ...lines.slice(end)];
  // Collapse a doubled blank line only at the cut point.
  if (at > 0 && rest[at - 1] === '' && rest[at] === '') rest.splice(at, 1);
  return { text: rest.join('\n'), cut };
}

const STYLE = { red: 'fg = "#f7768e", bold = true', amber: 'fg = "#e0af68"', green: 'fg = "#9ece6a"', dim: 'dim = true' };
const q = (s) => JSON.stringify(s);
const styled = (token, pairs) =>
  `{ token = ${q(token)}, rules = [${pairs.map(([v, s]) => `{ equals = ${q(v)}, ${STYLE[s] ?? `fg = ${q(s)}`} }`).join(', ')}] }`;

function block(adapter) {
  const { SPINNER } = require('./daemon');
  const state = styled('state_text', [['blocked', 'red'], ['working', 'amber'], ['done', 'green'], ['idle', 'dim'], ['unknown', 'dim']]);
  const role = styled(adapter.roleToken, adapter.rules.role);
  const mark = styled('$mark', [['✓', 'green'], ['?', 'red'], ...SPINNER.map((f) => [f, 'amber'])]);
  const stage = styled('$stage', adapter.rules.stage);
  const verdict = styled('$verdict', adapter.rules.verdict);
  const logo = styled('$logo', logoRules());
  const name = '{ token = "$name", fg = "#c0caf5", dim = false }';
  return [
    FENCE.start,
    '# One agent layout for lane and non-lane panes; unreported tokens hide.',
    '[ui.sidebar.agents]',
    'row_gap = 0',
    'rows = [',
    `  ["state_icon", ${logo}, ${name}, ${role}],`,
    `  [${mark}, ${state}, ${stage}, { token = "$round", dim = true }, ${verdict}, { token = "$title", dim = true }],`,
    ']',
    '',
    '[ui.sidebar.spaces]',
    'rows = [',
    `  ["state_icon", "workspace", ${state}, ${stage}],`,
    '  ["branch", "git_status"],',
    '  ["$usage"],',
    ']',
    FENCE.end,
  ].join('\n');
}

function herdrRun(...args) {
  const r = spawnSync(herdrBin(), args, { encoding: 'utf8', timeout: 10000 });
  return { ok: r.status === 0, out: `${r.stdout ?? ''}${r.stderr ?? ''}`.trim() };
}
const reload = () => herdrRun('server', 'reload-config').out;

// Write `next`, check it, restore `original` on failure.
function commit(file, original, next) {
  fs.writeFileSync(file, next, 'utf8');
  const result = herdrRun('config', 'check');
  if (!result.ok) {
    fs.writeFileSync(file, original, 'utf8');
    throw new Error(`herdr config check failed, original restored: ${result.out}`);
  }
  return result.out;
}

function configure(stateDir, adapter, legacyFences = []) {
  const file = configFile();
  const original = fs.readFileSync(file, 'utf8');
  let text = original;
  for (const fence of [FENCE, ...legacyFences]) text = dropFence(text, fence);
  const saved = [];
  for (const header of TABLES) {
    const r = cutTable(text, header);
    text = r.text;
    if (r.cut) saved.push(r.cut);
  }
  const out = commit(file, original, appendBlock(text, block(adapter)));
  const notes = [];
  try {
    fs.writeFileSync(`${file}.bak-herdr-lanes`, original, { flag: 'wx' });
    notes.push(`herdr config: backup ${file}.bak-herdr-lanes`);
  } catch {
    // an earlier backup is the pre-plugin copy; keep it
  }
  if (saved.length) {
    fs.mkdirSync(stateDir, { recursive: true });
    fs.writeFileSync(path.join(stateDir, SAVED), `${saved.join('\n\n')}\n`, 'utf8');
  }
  return [...notes, `herdr config: managed sidebar block written (${out})`, `reload: ${reload()}`];
}

function unconfigure(stateDir, legacyFences = []) {
  const file = configFile();
  const original = fs.readFileSync(file, 'utf8');
  let text = original;
  for (const fence of [FENCE, ...legacyFences]) text = dropFence(text, fence);
  if (text === original) return ['herdr config: no managed block'];
  const savedFile = path.join(stateDir, SAVED);
  if (fs.existsSync(savedFile)) text = appendBlock(text, fs.readFileSync(savedFile, 'utf8').replace(/\n+$/, ''));
  const out = commit(file, original, text);
  return [`herdr config: managed block removed, previous sidebar tables restored (${out})`, `reload: ${reload()}`];
}

module.exports = { FENCE, SAVED, configure, unconfigure, cutTable, dropFence, block };
