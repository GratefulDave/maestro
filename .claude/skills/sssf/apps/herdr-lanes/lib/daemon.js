'use strict';

// The resident daemon. Wakes on Herdr events (debounced) and on a slow poll,
// takes a fresh snapshot of panes, workspaces and the ledger, and writes only
// the tokens whose value changed. A spinner timer runs only while some pane
// is working, and rewrites only `mark`.

const herdr = require('./herdr');
const ledger = require('./ledger');

const POLL_MS = 2000;
const FRAME_MS = 200;
const TTL_MS = 180000; // tokens self-expire if the daemon dies
const REFRESH_MS = 60000; // re-send live tokens well inside the TTL
const SPINNER = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏'];

// Herdr agent id -> Private Use Area codepoint in Herdr Agent Icons Max
// (codepoint map from herdr-radar tools/codepoints.toml).
const LOGO = {
  claude: 0xe1a0, codex: 0xe1a1, opencode: 0xe1a2, omp: 0xe1a3, cline: 0xe1a4, mastracode: 0xe1a5,
  kimi: 0xe1a6, kilo: 0xe1a7, maki: 0xe1a8, pi: 0xe1a9, hermes: 0xe1aa, cursor: 0xe1ab, copilot: 0xe1ac,
  deepseek: 0xe1ad, gemini: 0xe1ae, gpt: 0xe1af, qwen: 0xe1b0, grok: 0xe1b1, agy: 0xe1b2, kiro: 0xe1b3,
  amp: 0xe1b4, devin: 0xe1b5, qodercli: 0xe1b6,
};

function logoFor(agent) {
  return agent && LOGO[agent] ? String.fromCodePoint(LOGO[agent]) : null;
}

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

function markText(mark, frame) {
  if (mark === 'working') return SPINNER[frame % SPINNER.length];
  if (mark === 'done') return '✓';
  if (mark === 'blocked') return '?';
  return null;
}

function laneOf(tokens) {
  return tokens?.lane && tokens?.run_id ? { lane: tokens.lane, run: tokens.run_id } : null;
}

function start({ log = (m) => process.stderr.write(`${new Date().toISOString()} ${m}\n`) } = {}) {
  const marks = new Map(); // pane_id -> held mark
  const written = { pane: new Map(), workspace: new Map() }; // id -> {token: value}
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
    const prev = written[kind].get(id) ?? {};
    const patch = {};
    for (const [name, value] of Object.entries(desired)) {
      if ((prev[name] ?? null) !== value || (refresh && value !== null)) patch[name] = value;
    }
    if (Object.keys(patch).length === 0) return;
    if (await herdr.report(kind, id, patch, TTL_MS)) written[kind].set(id, { ...prev, ...patch });
  }

  async function snapshot() {
    const [panes, workspaces] = await Promise.all([
      herdr.list('pane.list', 'panes'),
      herdr.list('workspace.list', 'workspaces'),
    ]);
    if (!panes || !workspaces) return; // failed read says nothing; try next tick
    const runIds = new Set();
    for (const p of panes) if (laneOf(p.tokens)) runIds.add(p.tokens.run_id);
    for (const w of workspaces) if (laneOf(w.tokens)) runIds.add(w.tokens.run_id);
    const fresh = ledger.readLanes([...runIds]);
    if (fresh) lanes = fresh; // busy ledger: keep last values
    const refresh = Date.now() - lastRefresh > REFRESH_MS;
    if (refresh) lastRefresh = Date.now();

    const livePanes = new Set();
    const jobs = [];
    for (const p of panes) {
      livePanes.add(p.pane_id);
      const mark = nextMark(marks.get(p.pane_id), p.agent ? p.agent_status : null, p.focused);
      marks.set(p.pane_id, mark);
      const lane = laneOf(p.tokens) && p.tokens.kind === 'lane' ? lanes.get(`${p.tokens.run_id}/${p.tokens.lane}`) : null;
      jobs.push(apply('pane', p.pane_id, {
        logo: logoFor(p.agent),
        mark: markText(mark, frame),
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
      const ref = laneOf(w.tokens);
      const lane = ref ? lanes.get(`${ref.run}/${ref.lane}`) : null;
      if (!lane && !written.workspace.has(w.workspace_id)) continue;
      jobs.push(apply('workspace', w.workspace_id, {
        stage: lane?.stage ?? null,
        round: lane?.round ?? null,
        verdict: lane?.verdict ?? null,
      }, refresh));
    }
    for (const id of [...written.workspace.keys()]) if (!liveSpaces.has(id)) written.workspace.delete(id);
    await Promise.all(jobs);
    scheduleFrames();
  }

  async function tick() {
    if (stopping) return;
    if (busy) { again = true; return; }
    busy = true;
    try {
      await snapshot();
    } catch (error) {
      log(`tick: ${error?.stack ?? error}`);
    } finally {
      busy = false;
      if (again) { again = false; wake(); }
    }
  }

  function wake() {
    clearTimeout(wakeTimer);
    wakeTimer = setTimeout(tick, 50);
  }

  function scheduleFrames() {
    const spinning = [...marks.values()].includes('working');
    if (spinning && !frameTimer) {
      frameTimer = setInterval(() => {
        frame += 1;
        for (const [id, mark] of marks) {
          if (mark === 'working') apply('pane', id, { mark: markText(mark, frame) }, false);
        }
      }, FRAME_MS);
    } else if (!spinning && frameTimer) {
      clearInterval(frameTimer);
      frameTimer = null;
    }
  }

  // Clear every token this daemon wrote, then exit.
  async function stop(code = 0) {
    if (stopping) return;
    stopping = true;
    subscription.stop();
    clearInterval(poll);
    clearInterval(frameTimer);
    clearTimeout(wakeTimer);
    const jobs = [];
    for (const kind of ['pane', 'workspace']) {
      for (const [id, tokens] of written[kind]) {
        const patch = Object.fromEntries(Object.keys(tokens).map((name) => [name, null]));
        if (Object.keys(patch).length) jobs.push(herdr.report(kind, id, patch));
      }
    }
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
  log(`started pid ${process.pid}; ledgers: ${ledger.ledgerPaths().join(', ') || 'none'}`);
  wake();
}

module.exports = { start, nextMark, markText, logoFor, SPINNER };
