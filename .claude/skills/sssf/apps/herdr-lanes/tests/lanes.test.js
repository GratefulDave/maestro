'use strict';

// node --test tests/lanes.test.js   (no dependencies)
// M1: a lock naming a live process that is not this daemon is never signalled.
// M2: N concurrent `ensure` runs leave exactly one daemon.
// M3: a multi-line rows table is cut whole; a failed config check leaves no saved fragment.

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const net = require('node:net');
const path = require('node:path');
const { spawn, execFile } = require('node:child_process');

const BIN = path.join(__dirname, '..', 'bin', 'lanes.js');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const alive = (pid) => { try { process.kill(pid, 0); return true; } catch { return false; } };

// A fake Herdr socket: empty lists, acks everything, keeps subscriptions open.
function fakeHerdr(sock) {
  const server = net.createServer((c) => {
    let body = '';
    c.on('data', (d) => {
      body += d;
      const nl = body.indexOf('\n');
      if (nl < 0) return;
      const req = JSON.parse(body.slice(0, nl));
      body = body.slice(nl + 1);
      c.write(`${JSON.stringify({ id: req.id, result: { panes: [], workspaces: [] } })}\n`);
      if (req.method !== 'events.subscribe') c.end();
    });
    c.on('error', () => {});
  });
  return new Promise((r) => server.listen(sock, () => r(server)));
}

function sandbox() {
  const dir = fs.mkdtempSync('/tmp/hl-');
  const env = {
    ...process.env,
    HERDR_SOCKET_PATH: path.join(dir, 's.sock'),
    HERDR_PLUGIN_STATE_DIR: path.join(dir, 'state'),
    XDG_CONFIG_HOME: path.join(dir, 'cfg'),
    MAESTRO_LANES_LEDGERS: path.join(dir, 'none.sqlite3'),
  };
  return { dir, env };
}

const run = (args, env) => new Promise((resolve) => {
  execFile(process.execPath, [BIN, ...args], { env }, (error, stdout, stderr) => resolve({ code: error?.code ?? 0, stdout, stderr }));
});

test('M1: stale lock owned by another process is not killed and does not block start', async (t) => {
  const { dir, env } = sandbox();
  const server = await fakeHerdr(env.HERDR_SOCKET_PATH);
  const other = spawn('/bin/sleep', ['30'], { stdio: 'ignore' });
  t.after(() => { other.kill(); server.close(); fs.rmSync(dir, { recursive: true, force: true }); });
  fs.mkdirSync(env.HERDR_PLUGIN_STATE_DIR, { recursive: true });
  fs.writeFileSync(path.join(env.HERDR_PLUGIN_STATE_DIR, 'daemon.lock'), String(other.pid));

  const stopped = await run(['--stop'], env);
  assert.match(stopped.stdout, /stale lock/);
  assert.ok(alive(other.pid), 'unrelated process must survive --stop');

  const started = await run([], env);
  assert.match(started.stdout, /daemon spawned/);
  await sleep(1500);
  const holder = Number(fs.readFileSync(path.join(env.HERDR_PLUGIN_STATE_DIR, 'daemon.lock'), 'utf8'));
  assert.notStrictEqual(holder, other.pid);
  assert.ok(alive(holder));
  assert.ok(alive(other.pid));
  process.kill(holder, 'SIGTERM');
  await sleep(500);
});

test('M2: concurrent ensure runs leave exactly one daemon', async (t) => {
  const { dir, env } = sandbox();
  const server = await fakeHerdr(env.HERDR_SOCKET_PATH);
  t.after(() => { server.close(); fs.rmSync(dir, { recursive: true, force: true }); });
  const results = await Promise.all(Array.from({ length: 8 }, () => run([], env)));
  const spawned = results.flatMap((r) => [...r.stdout.matchAll(/pid (\d+)/g)].map((m) => Number(m[1])));
  await sleep(2500);
  const survivors = spawned.filter(alive);
  assert.strictEqual(survivors.length, 1, `survivors ${survivors} of ${spawned}`);
  const holder = Number(fs.readFileSync(path.join(env.HERDR_PLUGIN_STATE_DIR, 'daemon.lock'), 'utf8'));
  assert.strictEqual(survivors[0], holder);
  process.kill(holder, 'SIGTERM');
  await sleep(500);
});

test('M3: multi-line table cut whole; failed check restores bytes and saves nothing', async (t) => {
  const { dir, env } = sandbox();
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  Object.assign(process.env, { XDG_CONFIG_HOME: env.XDG_CONFIG_HOME });
  const sidebar = require('../lib/sidebar');
  const adapter = require('../lib/adapters/maestro');

  const toml = 'a = 1\n\n[ui.sidebar.agents]\nrows = [\n  ["x"],\n["y"],\n]\n\n[theme]\nname = "t"\n';
  const { text, cut } = sidebar.cutTable(toml, '[ui.sidebar.agents]');
  assert.strictEqual(cut, '[ui.sidebar.agents]\nrows = [\n  ["x"],\n["y"],\n]');
  assert.strictEqual(text, 'a = 1\n\n[theme]\nname = "t"\n');

  const cfg = path.join(env.XDG_CONFIG_HOME, 'herdr', 'config.toml');
  fs.mkdirSync(path.dirname(cfg), { recursive: true });
  fs.writeFileSync(cfg, toml);
  const fake = path.join(dir, 'herdr-fail');
  fs.writeFileSync(fake, '#!/bin/sh\necho "config: issues found"\nexit 1\n', { mode: 0o755 });
  process.env.HERDR_BIN_PATH = fake;
  const state = path.join(dir, 'state');
  assert.throws(() => sidebar.configure(state, adapter), /config check failed/);
  assert.strictEqual(fs.readFileSync(cfg, 'utf8'), toml);
  assert.ok(!fs.existsSync(path.join(state, sidebar.SAVED)), 'no saved tables after rollback');
  assert.ok(!fs.existsSync(`${cfg}.bak-herdr-lanes`), 'no backup after rollback');

  fs.writeFileSync(fake, '#!/bin/sh\necho "config: ok"\n', { mode: 0o755 });
  sidebar.configure(state, adapter);
  assert.match(fs.readFileSync(path.join(state, sidebar.SAVED), 'utf8'), /\["y"\],/);
  sidebar.configure(state, adapter); // idempotent re-run keeps the saved tables
  assert.match(fs.readFileSync(path.join(state, sidebar.SAVED), 'utf8'), /\["y"\],/);
  sidebar.unconfigure(state);
  assert.match(fs.readFileSync(cfg, 'utf8'), /\[ui\.sidebar\.agents\]\nrows = \[\n {2}\["x"\],\n\["y"\],\n\]/);
  delete process.env.HERDR_BIN_PATH;
});
