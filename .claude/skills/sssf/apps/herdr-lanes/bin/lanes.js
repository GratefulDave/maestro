#!/usr/bin/env node
'use strict';

// herdr-lanes command line.
// (no args) start unless running | --daemon | --stop | --configure | --unconfigure
// | --install-font | --uninstall-font. See README.md.

const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawn, spawnSync } = require('node:child_process');

const herdr = require('../lib/herdr');
const adapter = require('../lib/adapters/maestro');

const stateDir = process.env.HERDR_PLUGIN_STATE_DIR ?? path.join(os.homedir(), '.local', 'state', 'herdr', 'plugins', 'herdr-lanes');
const lockFile = path.join(stateDir, 'daemon.lock');
const logFile = path.join(stateDir, 'daemon.log');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// A pid is ours only if it is alive AND its command line is this daemon.
// A recycled pid (after reboot, say) belongs to someone else and is never signalled.
function ownedPid(pid) {
  if (!(pid > 0)) return false;
  try { process.kill(pid, 0); } catch { return false; }
  const ps = spawnSync('/bin/ps', ['-o', 'command=', '-p', String(pid)], { encoding: 'utf8' });
  return /lanes\.js --daemon\b/.test(ps.stdout ?? '');
}

const readLock = () => { try { return fs.readFileSync(lockFile, 'utf8'); } catch { return null; } };
const lockHolder = () => { const pid = Number(readLock()); return ownedPid(pid) ? pid : null; };

// Exclusive lock: the content is written before the name exists (link is atomic
// and fails if the name is taken). A stale lock is renamed aside; if what was
// renamed turns out not to be the stale one we read, it is put back.
function acquireLock() {
  fs.mkdirSync(stateDir, { recursive: true });
  const tmp = `${lockFile}.${process.pid}`;
  fs.writeFileSync(tmp, String(process.pid), 'utf8');
  try {
    for (let attempt = 0; attempt < 5; attempt += 1) {
      try { fs.linkSync(tmp, lockFile); return true; } catch (e) { if (e.code !== 'EEXIST') throw e; }
      const seen = readLock();
      if (seen === null) continue;
      if (ownedPid(Number(seen))) return false;
      const aside = `${lockFile}.stale.${process.pid}`;
      try { fs.renameSync(lockFile, aside); } catch { continue; }
      if (fs.readFileSync(aside, 'utf8') !== seen) {
        try { fs.linkSync(aside, lockFile); } catch { /* a newer holder already re-took it */ }
        fs.unlinkSync(aside);
        return false;
      }
      fs.unlinkSync(aside);
    }
    return false;
  } finally {
    fs.unlinkSync(tmp);
  }
}

async function ensure() {
  for (const note of await require('../lib/migrate').migrate(stateDir, { ownedPid, sleep })) console.log(note);
  const pid = lockHolder();
  if (pid) return console.log(`herdr-lanes: daemon already running (pid ${pid})`);
  fs.mkdirSync(stateDir, { recursive: true });
  const err = fs.openSync(logFile, 'a');
  const child = spawn(process.execPath, [__filename, '--daemon'], { detached: true, stdio: ['ignore', err, err] });
  child.unref();
  console.log(`herdr-lanes: daemon spawned (pid ${child.pid}), log ${logFile}`);
}

function daemon() {
  if (!acquireLock()) process.exit(0);
  const mine = String(process.pid);
  process.on('exit', () => { if (readLock() === mine) { try { fs.unlinkSync(lockFile); } catch { /* gone */ } } });
  require('../lib/daemon').start({ adapter, ownsLock: () => readLock() === mine });
}

// Clear this plugin's token names on every target that shows one (source-scoped:
// only tokens reported under this plugin's source are affected).
async function sweep() {
  const [panes, workspaces] = await Promise.all([herdr.list('pane.list', 'panes'), herdr.list('workspace.list', 'workspaces')]);
  const clear = (names) => Object.fromEntries(names.map((n) => [n, null]));
  const has = (item, names) => names.some((n) => item.tokens && n in item.tokens);
  const results = await Promise.all([
    ...(panes ?? []).filter((p) => has(p, herdr.PANE_TOKENS)).map((p) => herdr.report('pane', p.pane_id, clear(herdr.PANE_TOKENS))),
    ...(workspaces ?? []).filter((w) => has(w, herdr.WORKSPACE_TOKENS)).map((w) => herdr.report('workspace', w.workspace_id, clear(herdr.WORKSPACE_TOKENS))),
  ]);
  return results.filter(Boolean).length;
}

async function stop() {
  const pid = lockHolder();
  if (pid) {
    process.kill(pid, 'SIGTERM');
    for (let i = 0; i < 80 && ownedPid(pid); i += 1) await sleep(100);
    console.log(ownedPid(pid) ? `herdr-lanes: pid ${pid} did not exit` : `herdr-lanes: stopped pid ${pid}`);
  } else if (readLock() !== null) console.log('herdr-lanes: stale lock (pid not ours); nothing signalled');
  console.log(`herdr-lanes: cleared own tokens on ${await sweep()} targets`);
}

async function main() {
  const { LEGACY_FENCES, migrate } = require('../lib/migrate');
  const run = {
    undefined: async () => { await ensure(); return []; },
    '--daemon': async () => { daemon(); return []; },
    '--stop': async () => { await stop(); return []; },
    '--configure': async () => [...await migrate(stateDir, { ownedPid, sleep }), ...require('../lib/sidebar').configure(stateDir, adapter, LEGACY_FENCES)],
    '--unconfigure': async () => require('../lib/sidebar').unconfigure(stateDir, LEGACY_FENCES),
    '--install-font': async () => require('../lib/terminal').installFont(stateDir),
    '--uninstall-font': async () => require('../lib/terminal').uninstallFont(stateDir),
  }[process.argv[2]];
  if (!run) throw new Error(`unknown mode ${process.argv[2]}`);
  for (const line of await run()) console.log(line);
}

main().catch((error) => {
  console.error(`herdr-lanes: ${error?.message ?? error}`);
  process.exit(1);
});
