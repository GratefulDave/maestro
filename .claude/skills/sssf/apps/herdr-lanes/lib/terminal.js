'use strict';

// Icon font install (macOS user font dir) and the terminal edit that makes the
// font reachable. Only the terminal(s) hosting a running Herdr client are edited
// (override with HERDR_LANES_TERMINAL=wezterm,ghostty). Every change is recorded in
// <state>/installed.json, and uninstall reverses only what that record names.
// Font and codepoint-map approach adapted from herdr-radar lib/font.js (MIT).

const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const FAMILY = 'Herdr Agent Icons Max';
const FONT_FILE = 'HerdrAgentIconsMax-Regular.ttf';
const FONT_SRC = path.join(__dirname, '..', 'fonts', FONT_FILE);
const MARK = '-- herdr-lanes: font wrapped';
const GHOSTTY_FENCE = { start: '# >>> herdr-lanes font', end: '# <<< herdr-lanes font' };

const home = os.homedir();
const xdg = () => process.env.XDG_CONFIG_HOME ?? path.join(home, '.config');
const sha = (file) => crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
const recordFile = (stateDir) => path.join(stateDir, 'installed.json');

const readRecord = (dir) => { try { return JSON.parse(fs.readFileSync(recordFile(dir), 'utf8')); } catch { return { terminals: {} }; } };
const writeRecord = (dir, record) => { fs.mkdirSync(dir, { recursive: true }); fs.writeFileSync(recordFile(dir), `${JSON.stringify(record, null, 2)}\n`); };
const backupOnce = (file) => { try { fs.copyFileSync(file, `${file}.bak-herdr-lanes`, fs.constants.COPYFILE_EXCL); } catch { /* keep first */ } };

// Terminals that are ancestors of a running `herdr` client process.
function detectTerminals() {
  if (process.env.HERDR_LANES_TERMINAL) return process.env.HERDR_LANES_TERMINAL.split(',').filter(Boolean);
  const ps = spawnSync('/bin/ps', ['-axo', 'pid=,ppid=,comm='], { encoding: 'utf8' });
  const procs = new Map();
  for (const line of (ps.stdout ?? '').split('\n')) {
    const m = line.trim().match(/^(\d+)\s+(\d+)\s+(.*)$/);
    if (m) procs.set(Number(m[1]), { ppid: Number(m[2]), comm: path.basename(m[3]) });
  }
  const found = new Set();
  for (const [pid, p] of procs) {
    if (p.comm !== 'herdr') continue;
    for (let cur = procs.get(pid), hops = 0; cur && hops < 20; cur = procs.get(cur.ppid), hops += 1) {
      if (cur.comm === 'wezterm-gui') found.add('wezterm');
      if (cur.comm === 'ghostty') found.add('ghostty');
    }
  }
  return [...found];
}

const TERMINALS = {
  wezterm: {
    file: () => path.join(xdg(), 'wezterm', 'wezterm.lua'),
    // The user's font comes first so it keeps the cell metrics; the icon font is only a
    // fallback for the codepoints the user's font lacks.
    install(text) {
      if (text.includes(MARK)) {
        // Migrate a line wrapped by 0.1.0 (icon font first); the recorded original is unchanged.
        const legacy = new RegExp(`font_with_fallback\\(\\{ "${FAMILY}", "([^"]+)" \\}\\) ${MARK}`);
        return { text: text.replace(legacy, (_, font) => `font_with_fallback({ "${font}", "${FAMILY}" }) ${MARK}`) };
      }
      const lines = text.split('\n');
      const hits = lines.map((l, i) => [l, i]).filter(([l]) => /^\s*config\.font\s*=/.test(l));
      const shape = /^(\s*)config\.font\s*=\s*wezterm\.font\(\s*(["'])([^"']+)\2\s*\)\s*$/;
      const m = hits.length === 1 && hits[0][0].match(shape);
      if (!m) {
        return { refused: `wezterm: config.font line not recognised; add by hand:\n  config.font = wezterm.font_with_fallback({ "<your font>", "${FAMILY}" })` };
      }
      const [original, index] = hits[0];
      lines[index] = `${m[1]}config.font = wezterm.font_with_fallback({ "${m[3]}", "${FAMILY}" }) ${MARK}`;
      return { text: lines.join('\n'), record: { line: index, original } };
    },
    uninstall(text, rec) {
      const lines = text.split('\n');
      const index = lines.findIndex((l) => l.includes(MARK));
      if (index < 0 || !rec?.original) return text;
      lines[index] = rec.original;
      return lines.join('\n');
    },
  },
  ghostty: {
    file: () => path.join(xdg(), 'ghostty', 'config'),
    install(text) {
      if (text.includes(GHOSTTY_FENCE.start)) return { text };
      const body = [['E1A0', 'E1B6'], ['E1C0', 'E1C5']].map(([a, b]) => `font-codepoint-map = U+${a}-U+${b}=${FAMILY}`);
      const addedNewline = !text.endsWith('\n') && text.length > 0;
      const next = `${text}${addedNewline ? '\n' : ''}${[GHOSTTY_FENCE.start, ...body, GHOSTTY_FENCE.end].join('\n')}\n`;
      return { text: next, record: { addedNewline } };
    },
    uninstall(text, rec) {
      const at = text.indexOf(GHOSTTY_FENCE.start);
      const endAt = text.indexOf(GHOSTTY_FENCE.end);
      if (at < 0 || endAt < 0) return text;
      const cut = at - (rec?.addedNewline && text[at - 1] === '\n' ? 1 : 0);
      return text.slice(0, cut) + text.slice(endAt + GHOSTTY_FENCE.end.length + 1);
    },
  },
};

function installFont(stateDir) {
  const notes = [];
  const record = readRecord(stateDir);
  const target = path.join(home, 'Library', 'Fonts', FONT_FILE);
  if (!fs.existsSync(target)) {
    fs.mkdirSync(path.dirname(target), { recursive: true });
    fs.copyFileSync(FONT_SRC, target);
    record.font = { path: target, sha256: sha(target) };
    notes.push(`font: installed ${target}`);
  } else notes.push(`font: ${target} already present${record.font ? '' : ' (not installed by this plugin; left alone on uninstall)'}`);

  const terminals = detectTerminals();
  if (terminals.length === 0) notes.push(`terminal: none detected; map U+E1A0-U+E1B6 and U+E1C0-U+E1C5 to "${FAMILY}" by hand`);
  for (const name of terminals) {
    const t = TERMINALS[name];
    const file = t?.file();
    if (!t || !fs.existsSync(file)) { notes.push(`${name}: no config file; skipped`); continue; }
    const text = fs.readFileSync(file, 'utf8');
    const result = t.install(text);
    if (result.refused) { notes.push(result.refused); continue; }
    if (result.text === text) { notes.push(`${name}: already configured`); continue; }
    backupOnce(file);
    fs.writeFileSync(file, result.text, 'utf8');
    record.terminals[name] = { ...record.terminals[name], file, ...result.record };
    notes.push(`${name}: font wired in ${file} (backup ${file}.bak-herdr-lanes); restart ${name} to load it`);
  }
  writeRecord(stateDir, record);
  return notes;
}

function uninstallFont(stateDir) {
  const notes = [];
  const record = readRecord(stateDir);
  for (const [name, rec] of Object.entries(record.terminals)) {
    if (!fs.existsSync(rec.file)) continue;
    const text = fs.readFileSync(rec.file, 'utf8');
    const next = TERMINALS[name].uninstall(text, rec);
    if (next !== text) { fs.writeFileSync(rec.file, next, 'utf8'); notes.push(`${name}: restored ${rec.file}`); }
    delete record.terminals[name];
  }
  if (record.font && fs.existsSync(record.font.path) && sha(record.font.path) === record.font.sha256) {
    fs.renameSync(record.font.path, path.join(os.tmpdir(), `${FONT_FILE}.removed-${Date.now()}`));
    notes.push(`font: moved ${record.font.path} out of ~/Library/Fonts`);
  }
  delete record.font;
  writeRecord(stateDir, record);
  return notes.length ? notes : ['font: nothing recorded as installed by this plugin'];
}

module.exports = { FAMILY, FONT_SRC, installFont, uninstallFont, readRecord, writeRecord, sha, detectTerminals };
