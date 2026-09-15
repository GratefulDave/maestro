#!/usr/bin/env node
'use strict';

// maestro-lanes command line.
//
//   node bin/lanes.js                  start the daemon unless one is running (startup hook, watchdog)
//   node bin/lanes.js --daemon         be the daemon
//   node bin/lanes.js --stop           stop the daemon and clear every token this plugin owns
//   node bin/lanes.js --configure      write the managed sidebar block into Herdr's config.toml
//   node bin/lanes.js --unconfigure    remove it and restore the tables it replaced
//   node bin/lanes.js --install-font   install the icon font and Ghostty codepoint map
//   node bin/lanes.js --uninstall-font remove both

const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawn } = require('node:child_process');

const herdr = require('../lib/herdr');

const stateDir = process.env.HERDR_PLUGIN_STATE_DIR ?? path.join(os.homedir(), '.local', 'state', 'herdr', 'plugins', herdr.SOURCE);
const pidFile = path.join(stateDir, 'daemon.pid');
const logFile = path.join(stateDir, 'daemon.log');

function runningPid() {
  try {
    const pid = Number(fs.readFileSync(pidFile, 'utf8'));
    if (pid > 0) { process.kill(pid, 0); return pid; }
  } catch {
    // no pid file, or the process is gone
  }
  return null;
}

function ensure() {
  const pid = runningPid();
  if (pid) return console.log(`maestro-lanes: daemon already running (pid ${pid})`);
  fs.mkdirSync(stateDir, { recursive: true });
  const err = fs.openSync(logFile, 'a');
  const child = spawn(process.execPath, [__filename, '--daemon'], { detached: true, stdio: ['ignore', err, err] });
  child.unref();
  console.log(`maestro-lanes: daemon started (pid ${child.pid}), log ${logFile}`);
}

function daemon() {
  if (runningPid() && runningPid() !== process.pid) process.exit(0);
  fs.mkdirSync(stateDir, { recursive: true });
  fs.writeFileSync(pidFile, String(process.pid), 'utf8');
  process.on('exit', () => {
    try {
      if (fs.readFileSync(pidFile, 'utf8') === String(process.pid)) fs.unlinkSync(pidFile);
    } catch {
      // already gone
    }
  });
  require('../lib/daemon').start();
}

// Clear this plugin's token names on every pane and workspace. Source-scoped:
// only tokens reported under source maestro-lanes are affected.
async function sweep() {
  const [panes, workspaces] = await Promise.all([
    herdr.list('pane.list', 'panes'),
    herdr.list('workspace.list', 'workspaces'),
  ]);
  const clear = (names) => Object.fromEntries(names.map((n) => [n, null]));
  // Only targets showing one of our names: a report claims a per-pane source slot.
  const has = (item, names) => names.some((n) => item.tokens && n in item.tokens);
  const jobs = [
    ...(panes ?? []).filter((p) => has(p, herdr.PANE_TOKENS)).map((p) => herdr.report('pane', p.pane_id, clear(herdr.PANE_TOKENS))),
    ...(workspaces ?? []).filter((w) => has(w, herdr.WORKSPACE_TOKENS)).map((w) => herdr.report('workspace', w.workspace_id, clear(herdr.WORKSPACE_TOKENS))),
  ];
  const results = await Promise.all(jobs);
  return results.filter(Boolean).length;
}

async function stop() {
  const pid = runningPid();
  if (pid) {
    process.kill(pid, 'SIGTERM');
    const deadline = Date.now() + 8000;
    while (runningPid() && Date.now() < deadline) await new Promise((r) => setTimeout(r, 100));
    console.log(runningPid() ? `maestro-lanes: pid ${pid} did not exit` : `maestro-lanes: stopped pid ${pid}`);
  }
  console.log(`maestro-lanes: cleared own tokens on ${await sweep()} targets`);
}

async function main() {
  const mode = process.argv[2];
  const setup = () => require('../lib/setup');
  if (!mode) return ensure();
  if (mode === '--daemon') return daemon();
  if (mode === '--stop') return stop();
  const notes = {
    '--configure': () => setup().configure(stateDir),
    '--unconfigure': () => setup().unconfigure(stateDir),
    '--install-font': () => setup().installFont(),
    '--uninstall-font': () => setup().uninstallFont(),
  }[mode];
  if (!notes) throw new Error(`unknown mode ${mode}`);
  for (const line of notes()) console.log(line);
}

main().catch((error) => {
  console.error(`maestro-lanes: ${error?.message ?? error}`);
  process.exit(1);
});
