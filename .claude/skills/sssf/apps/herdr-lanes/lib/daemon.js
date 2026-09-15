'use strict';

// The resident daemon. Wakes on Herdr events (debounced) and on a slow poll,
// takes a fresh snapshot of panes, workspaces and the lane adapter, and writes
// only the tokens whose value changed. A spinner timer runs only while some
// pane is working. A target with a report in flight is skipped until it lands,
// so a stalled server never builds a queue.

const herdr = require('./herdr');

const POLL_MS = 2000;
const FRAME_MS = 200;
const TTL_MS = 180000; // tokens self-expire if the daemon dies
const REFRESH_MS = 60000; // re-send live tokens well inside the TTL
const TITLE_MAX = 40;
const SPINNER = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏'];

// Herdr agent id -> Private Use Area glyph in Herdr Agent Icons Max, U+E1A0 upward
// (codepoint order from herdr-radar tools/codepoints.toml).
const LOGO_ORDER = ('claude codex opencode omp cline mastracode kimi kilo maki pi hermes cursor copilot deepseek '
  + 'gemini gpt qwen grok agy kiro amp devin qodercli').split(' ');
const logoFor = (agent) => (LOGO_ORDER.includes(agent) ? String.fromCodePoint(0xe1a0 + LOGO_ORDER.indexOf(agent)) : null);

const truncate = (text, max) => (!text ? null : [...text].length > max ? `${[...text].slice(0, max - 1).join('')}…` : text);

// Held activity mark: working spins; done is held until the pane is focused;
// blocked is held until the agent works again.
function nextMark(prev, status, focused) {
  if (status === 'working') return 'working';
  if (status === 'blocked') return 'blocked';
  if (status === 'done') return focused ? null : 'done';
  if (prev === 'working') return focused ? null : 'done';
  if (prev === 'done' && focused) return null;
  return prev ?? null;
}

const markText = (mark, frame) => (mark === 'working' ? SPINNER[frame % SPINNER.length] : { done: '✓', blocked: '?' }[mark] ?? null);

function start({ adapter, ownsLock = () => true, log = (m) => process.stderr.write(`${new Date().toISOString()} ${m}\n`) }) {
  const marks = new Map(); // pane_id -> held mark
  const written = { pane: new Map(), workspace: new Map() }; // id -> {token: value}
  const inflight = new Set(); // "kind:id"
  let lanes = new Map();
  let frame = 0;
  let lastRefresh = Date.now();
  let busy = false;
  let again = false;
  let stopping = false;
  let wakeTimer = null;
  let frameTimer = null;

  // Send the diff between what is on screen and `desired`; refresh re-sends live values.
  async function apply(kind, id, desired, refresh) {
    const slot = `${kind}:${id}`;
    if (inflight.has(slot) || stopping) return;
    const prev = written[kind].get(id) ?? {};
    const patch = {};
    for (const [name, value] of Object.entries(desired)) {
      if ((prev[name] ?? null) !== value || (refresh && value !== null)) patch[name] = value;
    }
    if (Object.keys(patch).length === 0) return;
    inflight.add(slot);
    const ok = await herdr.report(kind, id, patch, { ttlMs: TTL_MS }).catch(() => false);
    inflight.delete(slot);
    if (ok) written[kind].set(id, { ...(written[kind].get(id) ?? {}), ...patch });
  }

  async function snapshot() {
    const [panes, workspaces] = await Promise.all([
      herdr.list('pane.list', 'panes'),
      herdr.list('workspace.list', 'workspaces'),
    ]);
    if (!panes || !workspaces) return; // failed read says nothing; try next tick
    const labels = new Map(workspaces.map((w) => [w.workspace_id, w.label]));
    const keys = new Set();
    for (const p of panes) { const r = adapter.paneLane(p.tokens); if (r) keys.add(r.key); }
    for (const w of workspaces) { const r = adapter.spaceLane(w.tokens); if (r) keys.add(r.key); }
    const fresh = adapter.readLanes([...keys]);
    if (fresh) lanes = fresh; // busy source: keep last values
    const refresh = Date.now() - lastRefresh > REFRESH_MS;
    if (refresh) lastRefresh = Date.now();

    const jobs = [];
    const livePanes = new Set();
    for (const p of panes) {
      livePanes.add(p.pane_id);
      const ref = adapter.paneLane(p.tokens);
      if (!p.agent && !ref && !written.pane.has(p.pane_id)) continue;
      const mark = nextMark(marks.get(p.pane_id), p.agent ? p.agent_status : null, p.focused);
      marks.set(p.pane_id, mark);
      const lane = ref ? lanes.get(ref.key) : null;
      jobs.push(apply('pane', p.pane_id, {
        logo: logoFor(p.agent),
        mark: markText(mark, frame),
        name: ref ? ref.name : (p.agent ? (labels.get(p.workspace_id) ?? null) : null),
        title: ref || !p.agent ? null : truncate(p.terminal_title_stripped, TITLE_MAX),
        stage: lane?.stage ?? null,
        round: lane?.round ?? null,
        verdict: lane?.verdict ?? null,
      }, refresh));
    }
    // Vanished panes took their tokens with them; forget them.
    for (const id of [...marks.keys()]) if (!livePanes.has(id)) marks.delete(id);
    for (const id of [...written.pane.keys()]) if (!livePanes.has(id)) written.pane.delete(id);

    const liveSpaces = new Set();
    for (const w of workspaces) {
      liveSpaces.add(w.workspace_id);
      const ref = adapter.spaceLane(w.tokens);
      const lane = ref ? lanes.get(ref.key) : null;
      if (!lane && !written.workspace.has(w.workspace_id)) continue;
      jobs.push(apply('workspace', w.workspace_id, {
        stage: lane?.stage ?? null, round: lane?.round ?? null, verdict: lane?.verdict ?? null,
      }, refresh));
    }
    for (const id of [...written.workspace.keys()]) if (!liveSpaces.has(id)) written.workspace.delete(id);
    await Promise.all(jobs);
    scheduleFrames();
  }

  async function tick() {
    if (stopping) return;
    if (!ownsLock()) { log('lock held by another daemon; exiting without clearing'); process.exit(0); }
    if (busy) { again = true; return; }
    busy = true;
    try { await snapshot(); } catch (error) { log(`tick: ${error?.stack ?? error}`); }
    busy = false;
    if (again) { again = false; wake(); }
  }

  const wake = () => { clearTimeout(wakeTimer); wakeTimer = setTimeout(tick, 50); };

  function scheduleFrames() {
    const spinning = [...marks.values()].includes('working');
    if (spinning && !frameTimer) {
      frameTimer = setInterval(() => {
        frame += 1;
        for (const [id, mark] of marks) if (mark === 'working') apply('pane', id, { mark: markText(mark, frame) }, false);
      }, FRAME_MS);
    } else if (!spinning && frameTimer) {
      clearInterval(frameTimer);
      frameTimer = null;
    }
  }

  // Clear every token name this daemon may have written on every target it touched, then exit.
  async function stop(code = 0) {
    if (stopping) return;
    stopping = true;
    subscription.stop();
    clearInterval(poll);
    clearInterval(frameTimer);
    clearTimeout(wakeTimer);
    const targets = { pane: new Set(written.pane.keys()), workspace: new Set(written.workspace.keys()) };
    for (const slot of inflight) { const [kind, ...rest] = slot.split(':'); targets[kind].add(rest.join(':')); }
    const clear = (names) => Object.fromEntries(names.map((n) => [n, null]));
    const jobs = [
      ...[...targets.pane].map((id) => herdr.report('pane', id, clear(herdr.PANE_TOKENS))),
      ...[...targets.workspace].map((id) => herdr.report('workspace', id, clear(herdr.WORKSPACE_TOKENS))),
    ];
    await Promise.all(jobs);
    log(`stopped; cleared tokens on ${jobs.length} targets`);
    process.exit(code);
  }

  const subscription = herdr.subscribe(
    (reconnected) => {
      // A reconnect may be a new server that holds none of our tokens.
      if (reconnected) { written.pane.clear(); written.workspace.clear(); }
      wake();
    },
    () => { log('herdr socket gone; exiting'); process.exit(0); },
  );
  const poll = setInterval(tick, POLL_MS);
  process.on('SIGTERM', () => stop(0));
  process.on('SIGINT', () => stop(0));
  log(`started pid ${process.pid}; ${adapter.describe()}`);
  wake();
}

module.exports = { start, nextMark, markText, logoFor, SPINNER };
