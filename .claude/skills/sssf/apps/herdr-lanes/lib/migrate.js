'use strict';

// One-time migration from this plugin's first name, `maestro-lanes`
// (metadata source `maestro-lanes`, markers `# >>> maestro-lanes ...`).
// Idempotent; a marker file in the new state dir records completion.

const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const herdr = require('./herdr');
const terminal = require('./terminal');
const { configDir } = herdr;

const LEGACY = 'maestro-lanes';
const LEGACY_TOKENS = ['logo', 'mark', 'stage', 'round', 'verdict'];
const LEGACY_FENCES = [{ start: `# >>> ${LEGACY} sidebar`, end: `# <<< ${LEGACY} sidebar` }];
const LEGACY_FONT_FENCE = { start: `# >>> ${LEGACY} font`, end: `# <<< ${LEGACY} font` };

async function migrate(stateDir, { ownedPid, sleep }) {
  const done = path.join(stateDir, `migrated-from-${LEGACY}`);
  if (fs.existsSync(done)) return [];
  const notes = [];
  const legacyState = path.join(path.dirname(stateDir), LEGACY);

  // 1. Stop a legacy daemon we can prove is ours (it clears its own tokens).
  try {
    const pid = Number(fs.readFileSync(path.join(legacyState, 'daemon.pid'), 'utf8'));
    if (ownedPid(pid)) {
      process.kill(pid, 'SIGTERM');
      for (let i = 0; i < 80 && ownedPid(pid); i += 1) await sleep(100);
      notes.push(`migrate: stopped legacy daemon ${pid}`);
    }
  } catch { /* none */ }

  // 2. Clear anything still reported under the legacy source.
  const [panes, workspaces] = await Promise.all([herdr.list('pane.list', 'panes'), herdr.list('workspace.list', 'workspaces')]);
  if (!panes || !workspaces) throw new Error('migrate: herdr unreachable; retry');
  const clear = Object.fromEntries(LEGACY_TOKENS.map((n) => [n, null]));
  const shows = (item) => LEGACY_TOKENS.some((n) => item.tokens && n in item.tokens);
  const opts = { source: LEGACY, owned: LEGACY_TOKENS };
  const jobs = [...panes.filter(shows).map((p) => herdr.report('pane', p.pane_id, clear, opts)),
    ...workspaces.filter(shows).map((w) => herdr.report('workspace', w.workspace_id, { stage: null, round: null, verdict: null }, opts))];
  await Promise.all(jobs);
  notes.push(`migrate: cleared legacy source on ${jobs.length} targets`);

  // 3. Carry forward the pre-plugin backup, the cut sidebar tables and the font record.
  fs.mkdirSync(stateDir, { recursive: true });
  const copies = [
    [path.join(configDir(), `config.toml.bak-${LEGACY}`), path.join(configDir(), 'config.toml.bak-herdr-lanes')],
    [path.join(legacyState, 'replaced-sidebar-tables.toml'), path.join(stateDir, 'replaced-sidebar-tables.toml')],
  ];
  for (const [from, to] of copies) {
    if (fs.existsSync(from) && !fs.existsSync(to)) { fs.copyFileSync(from, to); notes.push(`migrate: ${from} -> ${to}`); }
  }
  const record = terminal.readRecord(stateDir);
  const font = path.join(os.homedir(), 'Library', 'Fonts', path.basename(terminal.FONT_SRC));
  if (!record.font && fs.existsSync(legacyState) && fs.existsSync(font) && terminal.sha(font) === terminal.sha(terminal.FONT_SRC)) {
    record.font = { path: font, sha256: terminal.sha(font) };
    terminal.writeRecord(stateDir, record);
    notes.push(`migrate: font ${font} recorded as installed by this plugin`);
  }

  // 4. Drop the legacy Ghostty block if one is still there.
  const ghostty = path.join(process.env.XDG_CONFIG_HOME ?? path.join(os.homedir(), '.config'), 'ghostty', 'config');
  if (fs.existsSync(ghostty)) {
    const text = fs.readFileSync(ghostty, 'utf8');
    const at = text.indexOf(LEGACY_FONT_FENCE.start);
    const end = text.indexOf(LEGACY_FONT_FENCE.end);
    if (at >= 0 && end > at) {
      const before = text.slice(0, at).replace(/\n\n$/, '');
      fs.writeFileSync(ghostty, before + text.slice(end + LEGACY_FONT_FENCE.end.length).replace(/^\n/, ''), 'utf8');
      notes.push(`migrate: removed legacy block from ${ghostty}`);
    }
  }
  fs.writeFileSync(done, `${new Date().toISOString()}\n`, 'utf8');
  return notes;
}

module.exports = { migrate, LEGACY_FENCES };
