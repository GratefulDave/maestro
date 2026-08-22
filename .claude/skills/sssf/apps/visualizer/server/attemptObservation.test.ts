import { describe, expect, test } from "bun:test";
import { spawnSync } from "node:child_process";
import {
  observeAttemptLiveness,
  sameSchedulerHost,
} from "./attemptObservation.ts";

function deadPid(): number {
  const child = spawnSync("sh", ["-c", "echo $$"], { encoding: "utf8" });
  const pid = Number((child.stdout ?? "").trim());
  if (!Number.isInteger(pid) || pid <= 0) {
    throw new Error("could not allocate a dead pid");
  }
  return pid;
}

describe("observeAttemptLiveness", () => {
  test("RUNNING + live pid → running", () => {
    expect(observeAttemptLiveness("RUNNING", process.pid)).toEqual({
      running: true,
      liveness: "running",
    });
  });

  test("RUNNING + dead pid → stale", () => {
    expect(observeAttemptLiveness("RUNNING", deadPid())).toEqual({
      running: false,
      liveness: "stale",
    });
  });

  test("RUNNING + no pid → not recorded", () => {
    expect(observeAttemptLiveness("RUNNING", null)).toEqual({
      running: false,
      liveness: "not_recorded",
    });
  });

  test("RUNNING + pid <= 0 → unknown, never probed", () => {
    expect(observeAttemptLiveness("RUNNING", 0)).toEqual({
      running: false,
      liveness: "unknown",
    });
    expect(observeAttemptLiveness("RUNNING", -1)).toEqual({
      running: false,
      liveness: "unknown",
    });
  });

  test("RUNNING + live pid on another host → unknown, not running", () => {
    expect(observeAttemptLiveness("RUNNING", process.pid, "other-box", "this-box")).toEqual({
      running: false,
      liveness: "unknown",
    });
  });

  test("RUNNING + live pid on this host, FQDN vs short label → running", () => {
    expect(
      observeAttemptLiveness("RUNNING", process.pid, "Mac.attlocal.net", "Mac"),
    ).toEqual({
      running: true,
      liveness: "running",
    });
  });

  test("empty recorded host still probes this machine", () => {
    expect(observeAttemptLiveness("RUNNING", process.pid, "", "this-box")).toEqual({
      running: true,
      liveness: "running",
    });
    expect(observeAttemptLiveness("RUNNING", process.pid, null, "this-box")).toEqual({
      running: true,
      liveness: "running",
    });
  });

  test("any non-RUNNING state → not running", () => {
    for (const state of ["CANCELLED", "MERGED", "FAILED", "READY", "BLOCKED"]) {
      expect(observeAttemptLiveness(state, process.pid)).toEqual({
        running: false,
        liveness: "not_running",
      });
      expect(observeAttemptLiveness(state, null)).toEqual({
        running: false,
        liveness: "not_running",
      });
      expect(observeAttemptLiveness(state, 0, "other-box", "this-box")).toEqual({
        running: false,
        liveness: "not_running",
      });
    }
  });
});

describe("sameSchedulerHost", () => {
  test("short label matches FQDN case-insensitively", () => {
    expect(sameSchedulerHost("Mac.attlocal.net", "mac")).toBe(true);
    expect(sameSchedulerHost("Mac", "OtherBox")).toBe(false);
    expect(sameSchedulerHost("", "Mac")).toBe(false);
    expect(sameSchedulerHost("   ", "Mac")).toBe(false);
  });
});
