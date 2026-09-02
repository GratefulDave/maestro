/**
 * The scheduler's per-step progress log.
 *
 * A stage like REVIEWING_CODE is one `lane_state.stage` row for minutes at a
 * time while the scheduler provisions a tree, runs a sealed suite, dispatches
 * an agent and waits on it. The ledger records the transition at the end, so a
 * dashboard reading only the ledger shows an unchanged row throughout and an
 * operator reads a working lane as a stuck one.
 *
 * So the scheduler also appends a line per step to `steps.jsonl`, beside the
 * ledger in the same `runtime_state_root`. This is a **narration**, not
 * workflow authority: nothing here decides a stage, and the dashboard never
 * writes it. Where the two disagree the ledger is right.
 *
 * The file is append-only and never truncated, which is what lets a reader
 * poll it by byte offset — the cursor a page returns is the offset the next
 * page starts at, so a poll loop re-sends nothing it has already seen. Three
 * things follow from that and are the whole reason this module is not a
 * `readFileSync().split("\n")`:
 *
 *   - a reader can catch the writer mid-append, so a trailing line without its
 *     newline is not yet a line and is left for the next poll;
 *   - a line that does not parse is skipped rather than failing the request —
 *     one malformed record must not cost the operator the narration;
 *   - a first read of a long-running run starts near the end rather than
 *     shipping megabytes the UI would immediately discard.
 */
import { closeSync, fstatSync, openSync, readSync } from "node:fs";
import { dirname, join } from "node:path";
import type { StepLine, StepLogPage } from "../shared/types.ts";

/** The file's name inside the runtime state root. */
export const STEP_LOG_FILE = "steps.jsonl";

const NEWLINE = 0x0a;

/**
 * How much of the file one request may read.
 *
 * Bounds both the tail a cold reader starts from and how far a resuming one
 * catches up in a single poll; `has_more` tells the client to poll again
 * immediately rather than wait for its next tick.
 */
const DEFAULT_MAX_BYTES = 256 * 1024;

/**
 * The step log beside a ledger.
 *
 * Derived, never configured: the writer puts it in the same directory as
 * `lifecycle.sqlite3`, so the dashboard already knows where it is the moment
 * it knows which ledger it is serving.
 */
export function stepLogPathFor(ledgerPath: string): string {
  return join(dirname(ledgerPath), STEP_LOG_FILE);
}

/**
 * One record, or `null` if the line is not one.
 *
 * `run_id` and `message` are the two fields the UI cannot render without, so a
 * line missing either is dropped. `detail` is always present in the contract
 * but is defaulted anyway — an absent detail is an empty one, not an error —
 * and a run-level line carries `lane_id` `"-"`.
 */
export function parseStepLine(line: string): StepLine | null {
  let value: unknown;
  try {
    value = JSON.parse(line);
  } catch {
    return null;
  }
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const record = value as Record<string, unknown>;
  const runId = record.run_id;
  const message = record.message;
  if (typeof runId !== "string" || !runId) return null;
  if (typeof message !== "string") return null;
  const laneId = record.lane_id;
  const detail = record.detail;
  const at = record.ts;
  return {
    ts: typeof at === "string" && at ? at : null,
    run_id: runId,
    lane_id: typeof laneId === "string" && laneId ? laneId : "-",
    message,
    detail: typeof detail === "string" ? detail : "",
  };
}

export class StepLogReader {
  readonly path: string;
  private readonly maxBytes: number;

  constructor(path: string, maxBytes = DEFAULT_MAX_BYTES) {
    this.path = path;
    this.maxBytes = Math.max(1, maxBytes);
  }

  /**
   * Lines after byte offset `after`, optionally only one run's.
   *
   * The cursor is a byte offset into the file and always advances over whole
   * lines only, so it is meaningful regardless of `runId`: filtering drops
   * records from the page, never bytes from the cursor.
   *
   * A file that has not been created yet answers `present: false` with no
   * steps. That is the ordinary state of a run whose scheduler has not
   * narrated anything yet, and the UI is required to render it as "no steps"
   * rather than as a failure.
   */
  read(after = 0, runId: string | null = null): StepLogPage {
    let fd: number;
    try {
      fd = openSync(this.path, "r");
    } catch {
      return { present: false, steps: [], cursor: 0, has_more: false, size: 0 };
    }
    try {
      const size = fstatSync(fd).size;
      let start = Number.isFinite(after) && after > 0 ? Math.floor(after) : 0;
      // A cursor past the end means the file was replaced under us — the
      // contract says that does not happen, but reading from a stale offset
      // into a shorter file would silently answer nothing forever, so start
      // over rather than go quiet.
      if (start > size) start = 0;

      // A cold read of a long run starts near the end. The first line in that
      // window is almost certainly a fragment, so it is skipped.
      let tailing = false;
      if (start === 0 && size > this.maxBytes) {
        start = size - this.maxBytes;
        tailing = true;
      }

      const length = Math.min(size - start, this.maxBytes);
      if (length <= 0) {
        return { present: true, steps: [], cursor: start, has_more: false, size };
      }

      const buffer = Buffer.allocUnsafe(length);
      const got = readSync(fd, buffer, 0, length, start);
      const chunk = buffer.subarray(0, got);

      let from = 0;
      if (tailing) {
        const firstNewline = chunk.indexOf(NEWLINE);
        // No newline at all in the tail window: one line longer than the
        // window. Step past it rather than returning the same nothing forever.
        if (firstNewline === -1) {
          return {
            present: true,
            steps: [],
            cursor: start + got,
            has_more: start + got < size,
            size,
          };
        }
        from = firstNewline + 1;
      }

      // Whole lines only. Everything after the last newline is either a line
      // the writer has not finished appending or a fragment of one the next
      // window will carry, so it is left uncounted and uncommitted.
      const lastNewline = chunk.lastIndexOf(NEWLINE);
      if (lastNewline < from) {
        const overlong = got === this.maxBytes && start + got < size;
        return {
          present: true,
          steps: [],
          // An over-long line with no newline anywhere in a full window would
          // stall the cursor; skipping it costs one record and keeps the feed
          // moving. Otherwise hold position: the writer is mid-append.
          cursor: overlong ? start + got : start + from,
          has_more: start + got < size,
          size,
        };
      }

      const end = lastNewline + 1;
      // Sliced on byte boundaries that are newlines, so no multi-byte
      // character is ever cut in half by the decode.
      const text = chunk.subarray(from, end).toString("utf8");
      const cursor = start + end;

      const steps: StepLine[] = [];
      for (const line of text.split("\n")) {
        if (!line.trim()) continue;
        const step = parseStepLine(line);
        // A record we cannot read is skipped, not raised: the operator loses
        // one line of narration instead of the whole feed.
        if (step === null) continue;
        if (runId !== null && step.run_id !== runId) continue;
        steps.push(step);
      }

      // `has_more` means "the byte cap cut this page short", not "bytes remain
      // past the cursor". Those differ by exactly the line the writer is
      // mid-way through appending — and reading that as more-to-fetch has the
      // client re-request the same nothing as fast as it can until the writer
      // finishes the line.
      return { present: true, steps, cursor, has_more: start + got < size, size };
    } finally {
      closeSync(fd);
    }
  }
}
