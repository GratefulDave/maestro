'use strict';

// Maestro adapter. Everything specific to one lane factory lives here.
//
// Adapter interface (the core daemon and sidebar builder use nothing else):
//   describe()                -> string for the daemon log
//   paneLane(tokens)          -> { key, name } | null   pane tokens -> lane reference
//   spaceLane(tokens)         -> { key, name } | null   workspace tokens -> lane reference
//   readLanes(keys)           -> Map<key, { stage, round, verdict }> | null (null = source busy, keep last values)
//   rules                     -> { role: [[value, style]], stage: [[value, style]], verdict: [[value, style]] }
//                                style is 'red' | 'amber' | 'green' | 'dim' | '#rrggbb'
//   roleToken                 -> sidebar token holding the lane role, e.g. '$role'
//
// Conventions read here:
//   - Lane panes carry tokens kind=lane, lane=<lane_id>, run_id=<run> (written by Maestro,
//     never by this plugin). Lane Spaces carry workspace tokens lane and run_id.
//   - Ledger: <state>/maestro-artifact-factory/*/lifecycle.sqlite3, or MAESTRO_LANES_LEDGERS
//     (colon-separated). Each read opens with SQLITE_OPEN_READONLY and busy timeout 0, runs one
//     SELECT and closes; a busy database skips the tick. Only the typed `verdict` field is read
//     from review payloads.
//   - round = number of TEST_REVIEW + CODE_REVIEW artifacts for the lane in that run.

const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const REVIEW_KINDS = "('TEST_REVIEW', 'CODE_REVIEW')";

function ledgerPaths() {
  if (process.env.MAESTRO_LANES_LEDGERS) {
    return process.env.MAESTRO_LANES_LEDGERS.split(':').filter((file) => file && fs.existsSync(file));
  }
  const root = path.join(os.homedir(), '.local', 'state', 'maestro-artifact-factory');
  try {
    return fs.readdirSync(root).map((name) => path.join(root, name, 'lifecycle.sqlite3')).filter((f) => fs.existsSync(f));
  } catch {
    return [];
  }
}

function query(file, runIds) {
  const { DatabaseSync } = require('node:sqlite');
  const db = new DatabaseSync(file, { readOnly: true, timeout: 0 });
  try {
    const marks = runIds.map(() => '?').join(', ');
    return db
      .prepare(
        `SELECT s.run_id, s.lane_id, s.stage,
           (SELECT count(*) FROM lane_artifacts a
             WHERE a.run_id = s.run_id AND a.lane_id = s.lane_id AND a.artifact_kind IN ${REVIEW_KINDS}) AS round,
           (SELECT json_extract(a.payload_json, '$.verdict') FROM lane_artifacts a
             WHERE a.run_id = s.run_id AND a.lane_id = s.lane_id AND a.artifact_kind IN ${REVIEW_KINDS}
             ORDER BY a.sequence DESC LIMIT 1) AS verdict
         FROM lane_state s WHERE s.run_id IN (${marks})`,
      )
      .all(...runIds);
  } finally {
    db.close();
  }
}

const ref = (lane, run) => (lane && run ? { key: `${run}/${lane}`, name: lane } : null);

module.exports = {
  describe: () => `maestro ledgers: ${ledgerPaths().join(', ') || 'none'}`,
  paneLane: (t) => (t?.kind === 'lane' ? ref(t.lane, t.run_id) : null),
  spaceLane: (t) => ref(t?.lane, t?.run_id),
  roleToken: '$role',
  rules: {
    role: [['builder', '#7aa2f7'], ['code-reviewer', '#bb9af7'], ['tester', '#7dcfff'], ['test-reviewer', '#ff9e64']],
    stage: [
      ['WAITING_FOR_USER', 'red'], ['READY_TO_MERGE', 'green'], ['MERGED', 'green'],
      ['BUILDING', 'amber'], ['REVIEWING_TESTS', 'amber'], ['REVIEWING_CODE', 'amber'],
    ],
    verdict: [['REVISE', 'amber'], ['PASS', 'green']],
  },
  readLanes(keys) {
    const out = new Map();
    const runIds = [...new Set(keys.map((k) => k.split('/')[0]))];
    if (runIds.length === 0) return out;
    for (const file of ledgerPaths()) {
      try {
        for (const row of query(file, runIds)) {
          out.set(`${row.run_id}/${row.lane_id}`, {
            stage: row.stage,
            round: row.round > 0 ? `r${row.round}` : null,
            verdict: typeof row.verdict === 'string' ? row.verdict : null,
          });
        }
      } catch (error) {
        if (/busy|locked/i.test(String(error?.message))) return null;
        process.stderr.write(`ledger ${file}: ${error?.message}\n`);
      }
    }
    return out;
  },
};
