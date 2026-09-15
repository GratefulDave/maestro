'use strict';

// Read-only view of Maestro lifecycle ledgers: stage, review round, latest verdict.
//
// The ledger belongs to a live scheduler. Each read opens the file with
// SQLITE_OPEN_READONLY and busy timeout 0, runs one SELECT, and closes: no
// transaction is held between ticks, and a busy database skips the tick
// (the caller keeps the previous values) rather than waiting on the writer.
// Only the typed `verdict` field is extracted from a review payload.

const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const REVIEW_KINDS = "('TEST_REVIEW', 'CODE_REVIEW')";

function ledgerPaths() {
  if (process.env.MAESTRO_LANES_LEDGERS) return process.env.MAESTRO_LANES_LEDGERS.split(':').filter(Boolean);
  const root = path.join(os.homedir(), '.local', 'state', 'maestro-artifact-factory');
  try {
    return fs
      .readdirSync(root)
      .map((name) => path.join(root, name, 'lifecycle.sqlite3'))
      .filter((file) => fs.existsSync(file));
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

// Map "<run_id>/<lane_id>" -> {stage, round, verdict}, or null when any ledger
// was busy or unreadable this tick.
function readLanes(runIds) {
  const out = new Map();
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
}

module.exports = { ledgerPaths, readLanes };
