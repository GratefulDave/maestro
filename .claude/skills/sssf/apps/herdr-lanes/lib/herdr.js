'use strict';

// Herdr socket API: one request per connection, plus one long-lived event subscription
// used only as a wake hint. Adapted from herdr-radar lib/ipc.js + lib/subscribe.js (MIT).

const fs = require('node:fs');
const net = require('node:net');
const os = require('node:os');
const path = require('node:path');

const SOURCE = 'lanes';
// Every token name this plugin writes. Nothing else is ever set or cleared.
const PANE_TOKENS = ['logo', 'mark', 'name', 'title', 'stage', 'round', 'verdict'];
const WORKSPACE_TOKENS = ['stage', 'round', 'verdict'];
// Herdr 0.9.0 requires a pane_id on pane.agent_status_changed; status flips come from the poll.
const EVENT_KINDS = ['pane.created', 'pane.closed', 'pane.exited', 'pane.agent_detected', 'pane.focused',
  'workspace.created', 'workspace.closed', 'workspace.focused'];

const configDir = () => path.join(process.env.XDG_CONFIG_HOME ?? path.join(os.homedir(), '.config'), 'herdr');
const socketPath = () => process.env.HERDR_SOCKET_PATH ?? path.join(configDir(), 'herdr.sock');

let inFlight = 0;
const waiters = [];

// Parsed reply (which may carry `error`), or null on transport failure. At most 8 in flight.
async function call(method, params, timeoutMs = 4000) {
  if (inFlight >= 8) await new Promise((wake) => waiters.push(wake));
  inFlight += 1;
  try {
    return await new Promise((resolve) => {
      let body = '';
      const stream = net.connect({ path: socketPath() });
      const finish = (value) => { stream.destroy(); resolve(value); };
      stream.setTimeout(timeoutMs, () => finish(null));
      stream.on('error', () => finish(null));
      stream.on('connect', () => stream.write(`${JSON.stringify({ id: SOURCE, method, params })}\n`));
      stream.on('data', (chunk) => {
        body += chunk;
        const nl = body.indexOf('\n');
        if (nl < 0) return;
        try { finish(JSON.parse(body.slice(0, nl))); } catch { finish(null); }
      });
    });
  } finally {
    inFlight -= 1;
    waiters.shift()?.();
  }
}

// null on failure, never []: an empty list would read as "every pane is gone".
async function list(method, key) {
  const reply = await call(method, {});
  return !reply || reply.error ? null : (reply.result?.[key] ?? null);
}

// Monotonic across restarts (microsecond clock floor): a newer process is never "stale".
let lastSeq = 0;
const nextSeq = () => (lastSeq = Math.max(lastSeq + 1, Date.now() * 1000));

// tokens: name -> string | null (null clears). Refuses names outside `owned`.
async function report(kind, id, tokens, { ttlMs, source = SOURCE, owned } = {}) {
  const allowed = owned ?? (kind === 'pane' ? PANE_TOKENS : WORKSPACE_TOKENS);
  for (const name of Object.keys(tokens)) if (!allowed.includes(name)) throw new Error(`refusing foreign token ${name}`);
  const params = { [kind === 'pane' ? 'pane_id' : 'workspace_id']: id, source, tokens, seq: nextSeq() };
  if (ttlMs) params.ttl_ms = ttlMs;
  const reply = await call(`${kind}.report_metadata`, params);
  return Boolean(reply) && !reply.error;
}

// onWake(reconnected) per event batch; onGone() when the server socket vanished.
// Write-silent after the subscribe request: the server treats client bytes as a disconnect.
function subscribe(onWake, onGone) {
  let stopped = false;
  let attempts = 0;
  let current = null;
  const connect = () => {
    if (stopped) return;
    const stream = (current = net.connect({ path: socketPath() }));
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
    stream.on('connect', () => stream.write(`${JSON.stringify({ id: `${SOURCE}-sub`, method: 'events.subscribe',
      params: { subscriptions: EVENT_KINDS.map((type) => ({ type })) } })}\n`));
    stream.on('data', (chunk) => {
      body += chunk;
      const lines = body.split('\n');
      body = lines.pop();
      if (lines.length === 0 || stopped) return;
      const reconnected = !acked;
      if (!acked) {
        acked = true;
        if (lines[0].includes('"error"')) process.stderr.write(`events.subscribe refused: ${lines[0]}\n`);
        else attempts = 0; // a refusal keeps backing off
      }
      onWake(reconnected);
    });
    stream.on('error', retry);
    stream.on('close', retry);
  };
  connect();
  return { stop: () => { stopped = true; current?.destroy(); } };
}

module.exports = { SOURCE, PANE_TOKENS, WORKSPACE_TOKENS, configDir, call, list, report, subscribe };
