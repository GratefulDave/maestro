'use strict';

// Font install, Ghostty codepoint map, and the managed sidebar block in
// Herdr's config.toml. Every file edited is backed up once, and every edit is
// a marker-fenced block, so removal is exact.
// Font install and codepoint-map approach adapted from herdr-radar lib/font.js (MIT).

const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const { configDir } = require('./herdr');

const FONT_FAMILY = 'Herdr Agent Icons Max';
const FONT_FILE = 'HerdrAgentIconsMax-Regular.ttf';
const FONT_SRC = path.join(__dirname, '..', 'fonts', FONT_FILE);
const RANGES = [['E1A0', 'E1B6'], ['E1C0', 'E1C5']];
const BACKUP_SUFFIX = '.bak-maestro-lanes';

const herdrBin = () => process.env.HERDR_BIN_PATH ?? 'herdr';
const herdrConfig = () => path.join(configDir(), 'config.toml');
const ghosttyConfig = () => path.join(process.env.XDG_CONFIG_HOME ?? path.join(os.homedir(), '.config'), 'ghostty', 'config');

function fence(name) {
  return { start: `# >>> maestro-lanes ${name}`, end: `# <<< maestro-lanes ${name}` };
}

const esc = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
const blockPattern = (m) => new RegExp(`\\n*${esc(m.start)}[\\s\\S]*?${esc(m.end)}\\n*`);

function upsertBlock(text, m, body) {
  const block = `${m.start}\n${body}\n${m.end}`;
  if (blockPattern(m).test(text)) return text.replace(blockPattern(m), `\n\n${block}\n`);
  return `${text.replace(/\n+$/, '')}\n\n${block}\n`;
}

function dropBlock(text, m) {
  return text.replace(blockPattern(m), '\n').replace(/\n{3,}/g, '\n\n');
}

function backupOnce(file) {
  const backup = file + BACKUP_SUFFIX;
  if (!fs.existsSync(backup)) fs.copyFileSync(file, backup);
  return backup;
}

/* ---------------------------------------------------------------- font */

function fontTarget() {
  return path.join(os.homedir(), 'Library', 'Fonts', FONT_FILE);
}

function installFont() {
  const notes = [];
  const target = fontTarget();
  const same = fs.existsSync(target) && fs.readFileSync(target).equals(fs.readFileSync(FONT_SRC));
  if (!same) {
    fs.mkdirSync(path.dirname(target), { recursive: true });
    fs.copyFileSync(FONT_SRC, target);
    notes.push(`font: installed ${target}`);
  } else notes.push(`font: already installed at ${target}`);

  const file = ghosttyConfig();
  if (!fs.existsSync(file)) return [...notes, `ghostty: no config at ${file}; map U+E1A0-U+E1B6,U+E1C0-U+E1C5 to "${FONT_FAMILY}" by hand`];
  const text = fs.readFileSync(file, 'utf8');
  const body = RANGES.map(([a, b]) => `font-codepoint-map = U+${a}-U+${b}=${FONT_FAMILY}`).join('\n');
  const next = upsertBlock(text, fence('font'), body);
  if (next !== text) {
    notes.push(`ghostty: backup ${backupOnce(file)}`);
    fs.writeFileSync(file, next, 'utf8');
    notes.push(`ghostty: codepoint map written to ${file}; reopen Ghostty to load the font`);
  } else notes.push('ghostty: codepoint map already present');
  return notes;
}

function uninstallFont() {
  const notes = [];
  const target = fontTarget();
  if (fs.existsSync(target)) {
    fs.renameSync(target, path.join(os.tmpdir(), `${FONT_FILE}.removed-${Date.now()}`));
    notes.push(`font: moved ${target} out of ~/Library/Fonts`);
  }
  const file = ghosttyConfig();
  if (fs.existsSync(file)) {
    const text = fs.readFileSync(file, 'utf8');
    const next = dropBlock(text, fence('font'));
    if (next !== text) { fs.writeFileSync(file, next, 'utf8'); notes.push(`ghostty: codepoint map removed from ${file}`); }
  }
  return notes;
}

/* ------------------------------------------------------------- sidebar */

const q = (s) => JSON.stringify(s);
const rule = (value, style) => `{ equals = ${q(value)}, ${style} }`;
const styled = (token, rules) => `{ token = ${q(token)}, rules = [${rules.join(', ')}] }`;

const RED = 'fg = "#f7768e", bold = true';
const AMBER = 'fg = "#e0af68"';
const GREEN = 'fg = "#9ece6a"';
const DIM = 'dim = true';

function sidebarBlock() {
  const { SPINNER } = require('./daemon');
  const state = styled('state_text', [
    rule('blocked', RED), rule('working', AMBER), rule('done', GREEN), rule('idle', DIM), rule('unknown', DIM),
  ]);
  const role = styled('$role', [
    rule('builder', 'fg = "#7aa2f7"'), rule('code-reviewer', 'fg = "#bb9af7"'),
    rule('tester', 'fg = "#7dcfff"'), rule('test-reviewer', 'fg = "#ff9e64"'),
  ]);
  const mark = styled('$mark', [rule('✓', GREEN), rule('?', RED), ...SPINNER.map((f) => rule(f, AMBER))]);
  const stage = styled('$stage', [
    rule('WAITING_FOR_USER', RED), rule('READY_TO_MERGE', GREEN), rule('MERGED', GREEN),
    rule('BUILDING', AMBER), rule('REVIEWING_TESTS', AMBER), rule('REVIEWING_CODE', AMBER),
  ]);
  const verdict = styled('$verdict', [rule('REVISE', AMBER), rule('PASS', GREEN)]);
  return [
    '[ui.sidebar.agents]',
    'row_gap = 0',
    'rows = [',
    `  ["state_icon", "$logo", "workspace", "tab", ${role}],`,
    `  [${mark}, "agent", ${state}],`,
    `  [${stage}, { token = "$round", dim = true }, ${verdict}],`,
    '  [{ token = "terminal_title_stripped", dim = true }],',
    ']',
    '',
    '[ui.sidebar.spaces]',
    'rows = [',
    `  ["state_icon", "workspace", ${state}, ${stage}],`,
    '  ["branch", "git_status"],',
    '  ["$usage"],',
    ']',
  ].join('\n');
}

// Cut a top-level table (header line through the line before the next header).
function cutTable(text, header) {
  const lines = text.split('\n');
  const at = lines.findIndex((l) => l.trim() === header);
  if (at < 0) return { text, cut: '' };
  let end = at + 1;
  while (end < lines.length && !/^\s*\[/.test(lines[end])) end += 1;
  const cut = lines.slice(at, end).join('\n').replace(/\n+$/, '');
  return { text: [...lines.slice(0, at), ...lines.slice(end)].join('\n'), cut };
}

function configCheck() {
  const r = spawnSync(herdrBin(), ['config', 'check'], { encoding: 'utf8', timeout: 10000 });
  return { ok: r.status === 0, out: `${r.stdout ?? ''}${r.stderr ?? ''}`.trim() };
}

function reload() {
  const r = spawnSync(herdrBin(), ['server', 'reload-config'], { encoding: 'utf8', timeout: 10000 });
  return `${r.stdout ?? ''}${r.stderr ?? ''}`.trim();
}

function configure(stateDir) {
  const file = herdrConfig();
  const original = fs.readFileSync(file, 'utf8');
  const notes = [`herdr config: backup ${backupOnce(file)}`];
  // Drop our own block first so a re-run never mistakes it for hand-written tables.
  let text = dropBlock(original, fence('sidebar'));
  const saved = [];
  for (const header of ['[ui.sidebar.agents]', '[ui.sidebar.spaces]']) {
    const r = cutTable(text, header);
    text = r.text;
    if (r.cut) saved.push(r.cut);
  }
  if (saved.length) {
    fs.mkdirSync(stateDir, { recursive: true });
    fs.writeFileSync(path.join(stateDir, 'replaced-sidebar-tables.toml'), `${saved.join('\n\n')}\n`, 'utf8');
  }
  const next = upsertBlock(text.replace(/\n{3,}/g, '\n\n'), fence('sidebar'), sidebarBlock());
  fs.writeFileSync(file, next, 'utf8');
  const check = configCheck();
  if (!check.ok) {
    fs.writeFileSync(file, original, 'utf8');
    throw new Error(`herdr config check failed, original restored: ${check.out}`);
  }
  return [...notes, `herdr config: managed sidebar block written (${check.out})`, `reload: ${reload()}`];
}

function unconfigure(stateDir) {
  const file = herdrConfig();
  const original = fs.readFileSync(file, 'utf8');
  let text = dropBlock(original, fence('sidebar'));
  const savedFile = path.join(stateDir, 'replaced-sidebar-tables.toml');
  if (text !== original && fs.existsSync(savedFile)) text = `${text.replace(/\n+$/, '')}\n\n${fs.readFileSync(savedFile, 'utf8')}`;
  fs.writeFileSync(file, text, 'utf8');
  const check = configCheck();
  if (!check.ok) {
    fs.writeFileSync(file, original, 'utf8');
    throw new Error(`herdr config check failed, original restored: ${check.out}`);
  }
  return [`herdr config: managed block removed, previous sidebar tables restored (${check.out})`, `reload: ${reload()}`];
}

module.exports = { installFont, uninstallFont, configure, unconfigure, sidebarBlock };
