'use strict';

// Herdr socket API: one request per connection, plus one long-lived event
// subscription used only as a wake hint.
// Adapted from herdr-radar lib/ipc.js and lib/subscribe.js (MIT, see LICENSE-herdr-radar).

const fs = require('node:fs');
const net = require('node:net');
const os = require('node:os');
const path = require('node:path');

const SOURCE = 'maestro-lanes';
// Every token name this plugin writes. Nothing else is ever set or cleared.
const PANE_TOKENS = ['logo', 'mark', 'stage', 'round', 'verdict'];
const WORKSPACE_TOKENS = ['stage', 'round', 'verdict'];
const MAX_IN_FLIGHT = 8;

function configDir() {
  return path.join(process.env.XDG_CONFIG_HOME ?? path.join(os.homedir(), '.config'), 'herdr');
}

function socketPath() {
  return process.env.HERDR_SOCKET_PATH ?? path.join(configDir(), 'herdr.sock');
}

function rawCall(method, params, timeoutMs) {
  return new Promise((resolve) => {
    let body = '';
    let settled = false;
    const stream = net.connect({ path: socketPath() });
    const finish = (value) => {
      if (settled) return;
      settled = true;
      stream.destroy();
      resolve(value);
    };
    stream.setTimeout(timeoutMs, () => finish(null));
    stream.on('error', () => finish(null));
    stream.on('connect', () => stream.write(`${JSON.stringify({ id: SOURCE, method, params })}\n`));
    stream.on('data', (chunk) => {
      body += chunk;
      const nl = body.indexOf('\n');
      if (nl < 0) return;
      try {
        finish(JSON.parse(body.slice(0, nl)));
      } catch {
        finish(null);
      }
    });
  });
}

let inFlight = 0;
const waiters = [];

// Parsed reply (which may carry `error`), or null on transport failure.
async function call(method, params, timeoutMs = 4000) {
  if (inFlight >= MAX_IN_FLIGHT) await new Promise((wake) => waiters.push(wake));
  inFlight += 1;
  try {
    return await rawCall(method, params, timeoutMs);
  } finally {
    inFlight -= 1;
    waiters.shift()?.();
  }
}

// null on failure, never []: an empty list would read as "every pane is gone".
async function list(method, key) {
  const reply = await call(method, {});
  if (!reply || reply.error) return null;
  return reply.result?.[key] ?? null;
}

// tokens: name -> string | null (null clears). Refuses names this plugin does not own.
async function report(kind, id, tokens, ttlMs) {
  const owned = kind === 'pane' ? PANE_TOKENS : WORKSPACE_TOKENS;
  for (const name of Object.keys(tokens)) {
    if (!owned.includes(name)) throw new Error(`refusing to write foreign token ${name}`);
  }
  const target = kind === 'pane' ? { pane_id: id } : { workspace_id: id };
  const params = { ...target, source: SOURCE, tokens };
  if (ttlMs) params.ttl_ms = ttlMs;
  const reply = await call(`${kind}.report_metadata`, params);
  return Boolean(reply) && !reply.error;
}

// Herdr 0.9.0 requires a pane_id on pane.agent_status_changed, so status flips
// are picked up by the daemon's poll rather than subscribed to.
const EVENT_KINDS = [
  'pane.created',
  'pane.closed',
  'pane.exited',
  'pane.agent_detected',
  'pane.focused',
  'workspace.created',
  'workspace.closed',
  'workspace.focused',
];

// onWake(reconnected) on every event line; onGone() when the server socket vanished.
// Write-silent after the subscribe request: the server treats client bytes as a disconnect.
function subscribe(onWake, onGone) {
  let stopped = false;
  let attempts = 0;
  let current = null;
  const connect = () => {
    if (stopped) return;
    const stream = net.connect({ path: socketPath() });
    current = stream;
    let body = '';
    let acked = false;
    let ended = false;
    const retry = () => {
      if (ended) return;
      ended = true;
      stream.destroy();
      if (stopped) return;
      if (!fs.existsSync(socketPath())) return onGone();
      attempts += 1;
      setTimeout(connect, Math.min(30000, 500 * 2 ** Math.min(attempts, 6)));
    };
    stream.on('connect', () => {
      const subscriptions = EVENT_KINDS.map((type) => ({ type }));
      stream.write(`${JSON.stringify({ id: `${SOURCE}-sub`, method: 'events.subscribe', params: { subscriptions } })}\n`);
    });
    stream.on('data', (chunk) => {
      body += chunk;
      let woke = false;
      let reconnected = false;
      let nl;
      while ((nl = body.indexOf('\n')) >= 0) {
        const line = body.slice(0, nl);
        body = body.slice(nl + 1);
        if (!acked) {
          const refused = line.includes('"error"');
          if (refused) process.stderr.write(`events.subscribe refused: ${line}\n`);
          else attempts = 0; // a refusal keeps backing off
          acked = true;
          reconnected = true;
        }
        woke = true;
      }
      if (woke && !stopped) onWake(reconnected);
    });
    stream.on('error', retry);
    stream.on('close', retry);
  };
  connect();
  return { stop: () => { stopped = true; current?.destroy(); } };
}

module.exports = { SOURCE, PANE_TOKENS, WORKSPACE_TOKENS, configDir, call, list, report, subscribe };
