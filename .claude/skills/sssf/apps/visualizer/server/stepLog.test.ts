/**
 * The step-log reader against real files on disk.
 *
 * Every case here is a state a live scheduler actually puts the file in:
 * not created yet, created and empty, caught mid-append, carrying a record
 * this reader does not understand, and — the one the whole byte cursor exists
 * for — grown since the last poll.
 */
import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { appendFileSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { STEP_LOG_FILE, StepLogReader, parseStepLine, stepLogPathFor } from "./stepLog.ts";

const RUN = "f50638ab0000";
const OTHER_RUN = "aaaa11112222";

function line(over: Record<string, unknown> = {}): string {
  return `${JSON.stringify({
    ts: "2026-09-01T22:31:04.512+00:00",
    run_id: RUN,
    lane_id: "lane-wp7-build",
    message: "asking code reviewer",
    detail: "",
    ...over,
  })}\n`;
}

describe("step log reader", () => {
  let root = "";
  let path = "";

  beforeEach(() => {
    root = mkdtempSync(join(tmpdir(), "step-log-"));
    path = join(root, STEP_LOG_FILE);
  });

  afterEach(() => {
    rmSync(root, { recursive: true, force: true });
  });

  test("the log is derived from the ledger's own directory", () => {
    expect(stepLogPathFor("/state/fdadb/lifecycle.sqlite3")).toBe("/state/fdadb/steps.jsonl");
  });

  test("a file that does not exist is 'no steps', not an error", () => {
    const page = new StepLogReader(path).read();
    expect(page).toEqual({ present: false, steps: [], cursor: 0, has_more: false, size: 0 });
  });

  test("an empty file is present with nothing in it", () => {
    writeFileSync(path, "");
    const page = new StepLogReader(path).read();
    expect(page.present).toBe(true);
    expect(page.steps).toEqual([]);
    expect(page.cursor).toBe(0);
    expect(page.has_more).toBe(false);
  });

  test("reads whole lines and carries every contract field through", () => {
    writeFileSync(
      path,
      line({ message: "provisioning review tree", detail: "candidate 6302869444eb" }) +
        line({ message: "sealed suite FAILED", detail: "12 executed, 7 passed, 5 failed" }),
    );
    const page = new StepLogReader(path).read();
    expect(page.steps).toEqual([
      {
        ts: "2026-09-01T22:31:04.512+00:00",
        run_id: RUN,
        lane_id: "lane-wp7-build",
        message: "provisioning review tree",
        detail: "candidate 6302869444eb",
      },
      {
        ts: "2026-09-01T22:31:04.512+00:00",
        run_id: RUN,
        lane_id: "lane-wp7-build",
        message: "sealed suite FAILED",
        detail: "12 executed, 7 passed, 5 failed",
      },
    ]);
    expect(page.cursor).toBe(page.size);
    expect(page.has_more).toBe(false);
  });

  test("a run-level line keeps its '-' lane", () => {
    writeFileSync(path, line({ lane_id: "-", message: "run opened" }));
    expect(new StepLogReader(path).read().steps[0]!.lane_id).toBe("-");
  });

  /**
   * Pinned against records copied verbatim from a real run of
   * `adw_modules/step_log.py`, so a change to what the writer emits fails here
   * rather than quietly rendering wrong.
   *
   * `detail` is free text this reader carries and the UI displays. Nothing
   * parses it or keys on it — `run opened` carries the CLI verb that opened
   * the run, `run finished` carries the outcome, and a lane line carries
   * counts. A run-level line is identified by `lane_id === "-"`, never by its
   * message: reading `"run opened"` as the marker would break the moment the
   * writer adds a third run-level message.
   */
  test("the writer's own records, verbatim", () => {
    writeFileSync(
      path,
      '{"ts":"2026-09-01T22:34:06.273+00:00","run_id":"f50638ab","lane_id":"-",' +
        '"message":"run opened","detail":"start"}\n' +
        '{"ts":"2026-09-01T22:34:06.273+00:00","run_id":"f50638ab",' +
        '"lane_id":"lane-wp7-build","message":"sealed suite FAILED",' +
        '"detail":"12 executed, 7 passed, 5 failed, 0 errored"}\n' +
        '{"ts":"2026-09-01T22:34:06.273+00:00","run_id":"f50638ab","lane_id":"-",' +
        '"message":"run finished","detail":"COMPLETE"}\n',
    );
    const page = new StepLogReader(path).read(0, "f50638ab");
    expect(page.steps).toEqual([
      {
        ts: "2026-09-01T22:34:06.273+00:00",
        run_id: "f50638ab",
        lane_id: "-",
        message: "run opened",
        detail: "start",
      },
      {
        ts: "2026-09-01T22:34:06.273+00:00",
        run_id: "f50638ab",
        lane_id: "lane-wp7-build",
        message: "sealed suite FAILED",
        detail: "12 executed, 7 passed, 5 failed, 0 errored",
      },
      {
        ts: "2026-09-01T22:34:06.273+00:00",
        run_id: "f50638ab",
        lane_id: "-",
        message: "run finished",
        detail: "COMPLETE",
      },
    ]);
    expect(page.cursor).toBe(page.size);
    // The writer's timestamp is what the UI ages against, so it has to parse.
    expect(Number.isFinite(new Date(page.steps[0]!.ts!).getTime())).toBe(true);
  });

  test("each run-level verb comes through untouched", () => {
    writeFileSync(
      path,
      ["start", "resume", "amend"]
        .map((verb) => line({ lane_id: "-", message: "run opened", detail: verb }))
        .join(""),
    );
    expect(new StepLogReader(path).read().steps.map((s) => s.detail)).toEqual([
      "start",
      "resume",
      "amend",
    ]);
  });

  test("a trailing line without its newline is not yet a line", () => {
    const complete = line({ message: "asking code reviewer" });
    // Exactly what a reader sees when it lands between the writer's write and
    // its newline: the fragment must not be emitted, and the cursor must not
    // move past it, or the record is lost when it is finished.
    writeFileSync(path, complete + '{"ts":"2026-09-01T22:31:9');

    const reader = new StepLogReader(path);
    const first = reader.read();
    expect(first.steps).toHaveLength(1);
    expect(first.steps[0]!.message).toBe("asking code reviewer");
    expect(first.cursor).toBe(Buffer.byteLength(complete));
    // The fragment's bytes are ahead of the cursor, but `has_more` must stay
    // false: a client that read it as more-to-fetch would re-request the same
    // nothing at full speed for as long as the writer takes to finish the line.
    expect(first.has_more).toBe(false);

    // The writer finishes the line; the next poll picks up the whole record
    // and nothing is duplicated.
    writeFileSync(path, complete + line({ message: "code-reviewer replied", detail: "turn 16" }));
    const second = reader.read(first.cursor);
    expect(second.steps.map((s) => s.message)).toEqual(["code-reviewer replied"]);
    expect(second.has_more).toBe(false);
  });

  test("an unparseable line is skipped, the good ones around it are not", () => {
    writeFileSync(
      path,
      line({ message: "one" }) +
        "{not json at all\n" +
        line({ message: "two" }) +
        "[1,2,3]\n" +
        '{"run_id":"x"}\n' +
        line({ message: "three" }),
    );
    const page = new StepLogReader(path).read();
    expect(page.steps.map((s) => s.message)).toEqual(["one", "two", "three"]);
    // The bad lines are consumed, not left to be re-read forever.
    expect(page.cursor).toBe(page.size);
  });

  test("blank lines are ignored", () => {
    writeFileSync(path, `${line({ message: "one" })}\n\n${line({ message: "two" })}`);
    expect(new StepLogReader(path).read().steps.map((s) => s.message)).toEqual(["one", "two"]);
  });

  test("a cursor resumes exactly where the previous page stopped", () => {
    writeFileSync(path, line({ message: "one" }) + line({ message: "two" }));
    const reader = new StepLogReader(path);

    const first = reader.read();
    expect(first.steps.map((s) => s.message)).toEqual(["one", "two"]);

    // Nothing appended: the same cursor answers nothing rather than repeating.
    const idle = reader.read(first.cursor);
    expect(idle.steps).toEqual([]);
    expect(idle.cursor).toBe(first.cursor);

    appendFileSync(path, line({ message: "three" }));
    const second = reader.read(idle.cursor);
    expect(second.steps.map((s) => s.message)).toEqual(["three"]);
    expect(second.cursor).toBe(second.size);
  });

  test("only the asked-for run's lines come back, but the cursor counts bytes", () => {
    writeFileSync(
      path,
      line({ message: "mine" }) +
        line({ run_id: OTHER_RUN, message: "someone else's" }) +
        line({ message: "also mine" }),
    );
    const page = new StepLogReader(path).read(0, RUN);
    expect(page.steps.map((s) => s.message)).toEqual(["mine", "also mine"]);
    // Filtering drops records from the page, never bytes from the cursor —
    // otherwise the next poll would re-read the other run's line forever.
    expect(page.cursor).toBe(page.size);
  });

  test("a cold read of a long file starts near the end and reports more behind it", () => {
    const many = Array.from({ length: 400 }, (_, i) => line({ message: `step ${i}` })).join("");
    writeFileSync(path, many);
    const reader = new StepLogReader(path, 2048);

    const page = reader.read();
    expect(page.present).toBe(true);
    expect(page.steps.length).toBeGreaterThan(0);
    expect(page.steps.length).toBeLessThan(400);
    // The last line of the file is the newest thing that happened, which is
    // the whole point of tailing rather than truncating from the front.
    expect(page.steps.at(-1)!.message).toBe("step 399");
    // The fragment the window opened in the middle of is dropped, never
    // emitted as a mangled record.
    expect(page.steps.every((s) => s.message.startsWith("step "))).toBe(true);
    expect(page.cursor).toBe(page.size);
  });

  test("a reader that falls far behind catches up in bounded pages, losing nothing", () => {
    const reader = new StepLogReader(path, 1024);
    // A client that was watching from the start, so its cursor is real rather
    // than the cold-read tail.
    writeFileSync(path, line({ message: "step 0" }));
    const first = reader.read();
    expect(first.steps.map((s) => s.message)).toEqual(["step 0"]);

    // The run then produces far more than one window's worth between polls.
    appendFileSync(
      path,
      Array.from({ length: 199 }, (_, i) => line({ message: `step ${i + 1}` })).join(""),
    );

    const seen: string[] = [];
    let cursor = first.cursor;
    let guard = 0;
    let pages = 0;
    for (;;) {
      const page = reader.read(cursor);
      seen.push(...page.steps.map((s) => s.message));
      cursor = page.cursor;
      pages += 1;
      if (!page.has_more) break;
      if (++guard > 500) throw new Error("cursor did not advance");
    }
    // Bounded pages, so this genuinely took several — a single page would not
    // have exercised the cursor at all.
    expect(pages).toBeGreaterThan(1);
    expect(seen).toEqual(Array.from({ length: 199 }, (_, i) => `step ${i + 1}`));
  });

  test("the cold-read tail is only for a cold read", () => {
    const many = Array.from({ length: 400 }, (_, i) => line({ message: `step ${i}` })).join("");
    writeFileSync(path, many);
    const reader = new StepLogReader(path, 2048);
    // An explicit cursor is a client that has been watching, so it is honoured
    // rather than jumped forward to the end.
    const page = reader.read(Buffer.byteLength(line({ message: "step 0" })));
    expect(page.steps[0]!.message).toBe("step 1");
    expect(page.has_more).toBe(true);
  });

  test("a cursor past the end starts over instead of going silent", () => {
    writeFileSync(path, line({ message: "one" }));
    const page = new StepLogReader(path).read(1_000_000);
    expect(page.steps.map((s) => s.message)).toEqual(["one"]);
  });

  test("multi-byte characters survive a windowed read", () => {
    const many = Array.from({ length: 200 }, (_, i) =>
      line({ message: `étape ${i} — ✅` }),
    ).join("");
    writeFileSync(path, many);
    const page = new StepLogReader(path, 2048).read();
    expect(page.steps.at(-1)!.message).toBe("étape 199 — ✅");
    expect(page.steps.every((s) => !s.message.includes("�"))).toBe(true);
  });
});

describe("parseStepLine", () => {
  test("a record missing run_id or message is not a step", () => {
    expect(parseStepLine('{"message":"hi"}')).toBeNull();
    expect(parseStepLine(`{"run_id":"${RUN}"}`)).toBeNull();
    expect(parseStepLine('{"run_id":"","message":"hi"}')).toBeNull();
  });

  test("absent optional fields take their documented defaults", () => {
    expect(parseStepLine(`{"run_id":"${RUN}","message":"run opened"}`)).toEqual({
      ts: null,
      run_id: RUN,
      lane_id: "-",
      message: "run opened",
      detail: "",
    });
  });

  test("an empty message is a step; an empty detail is not a missing one", () => {
    expect(parseStepLine(`{"run_id":"${RUN}","message":"","detail":""}`)?.message).toBe("");
  });
});
