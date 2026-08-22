import { describe, expect, test } from "bun:test";
import { spawnSync } from "node:child_process";
import {
  observeAttemptLiveness,
  processStartEpoch,
  sameSchedulerHost,
} from "./attemptObservation.ts";

const HOST = "test-host";
const EPOCH = 100.5;
const PID = 4242;

function deadPid(): number {
  const child = spawnSync("sh", ["-c", "echo $$"], { encoding: "utf8" });
  const pid = Number((child.stdout ?? "").trim());
  if (!Number.isInteger(pid) || pid <= 0) {
    throw new Error("could not allocate a dead pid");
  }
  return pid;
}

function identity(overrides: {
  attemptHost?: string | null;
  attemptStartEpoch?: number | null;
  currentHost?: string | null;
  reviewDispatches?: unknown;
  declaredAccepted?: boolean;
  isAlive?: (pid: number) => boolean;
  startEpoch?: (pid: number) => number | null;
} = {}) {
  return {
    attemptHost: HOST,
    attemptStartEpoch: EPOCH,
    currentHost: HOST,
    isAlive: () => true,
    startEpoch: () => EPOCH,
    ...overrides,
  };
}

describe("observeAttemptLiveness", () => {
  test("RUNNING + matching live pid → running", () => {
    expect(observeAttemptLiveness("RUNNING", PID, identity())).toEqual({
      running: true,
      liveness: "running",
    });
  });

  test("RUNNING + matching dead pid → stale", () => {
    const aliveCalls: number[] = [];
    const epochCalls: number[] = [];
    expect(
      observeAttemptLiveness(
        "RUNNING",
        PID,
        identity({
          isAlive: (pid) => {
            aliveCalls.push(pid);
            return false;
          },
          startEpoch: (pid) => {
            epochCalls.push(pid);
            return EPOCH;
          },
        }),
      ),
    ).toEqual({
      running: false,
      liveness: "stale",
    });
    expect(aliveCalls).toEqual([PID]);
    expect(epochCalls).toEqual([]);
  });

  test("RUNNING + no pid → not recorded", () => {
    expect(observeAttemptLiveness("RUNNING", null, identity())).toEqual({
      running: false,
      liveness: "not_recorded",
    });
  });

  test("RUNNING + pid <= 0 → unknown, never probed", () => {
    const aliveCalls: number[] = [];
    const opts = identity({
      isAlive: (pid) => {
        aliveCalls.push(pid);
        return true;
      },
    });
    expect(observeAttemptLiveness("RUNNING", 0, opts)).toEqual({
      running: false,
      liveness: "unknown",
    });
    expect(observeAttemptLiveness("RUNNING", -1, opts)).toEqual({
      running: false,
      liveness: "unknown",
    });
    expect(aliveCalls).toEqual([]);
  });

  test("no host/epoch (old ledger) → unknown, not dead, not probed", () => {
    const aliveCalls: number[] = [];
    const epochCalls: number[] = [];
    const probe = {
      isAlive: (pid: number) => {
        aliveCalls.push(pid);
        return false;
      },
      startEpoch: (pid: number) => {
        epochCalls.push(pid);
        return EPOCH;
      },
      currentHost: HOST,
    };
    expect(
      observeAttemptLiveness("RUNNING", PID, {
        ...probe,
        attemptHost: null,
        attemptStartEpoch: null,
      }),
    ).toEqual({ running: false, liveness: "unknown" });
    expect(
      observeAttemptLiveness("RUNNING", PID, {
        ...probe,
        attemptHost: null,
        attemptStartEpoch: EPOCH,
      }),
    ).toEqual({ running: false, liveness: "unknown" });
    expect(
      observeAttemptLiveness("RUNNING", PID, {
        ...probe,
        attemptHost: HOST,
        attemptStartEpoch: null,
      }),
    ).toEqual({ running: false, liveness: "unknown" });
    expect(aliveCalls).toEqual([]);
    expect(epochCalls).toEqual([]);
  });

  test("RUNNING + live pid on another host → unknown, not running, not probed", () => {
    const aliveCalls: number[] = [];
    expect(
      observeAttemptLiveness(
        "RUNNING",
        PID,
        identity({
          attemptHost: "other-box",
          currentHost: "this-box",
          isAlive: (pid) => {
            aliveCalls.push(pid);
            return true;
          },
        }),
      ),
    ).toEqual({
      running: false,
      liveness: "unknown",
    });
    expect(aliveCalls).toEqual([]);
  });

  test("RUNNING + live pid on this host, FQDN vs short label → running", () => {
    expect(
      observeAttemptLiveness(
        "RUNNING",
        PID,
        identity({ attemptHost: "Mac.attlocal.net", currentHost: "Mac" }),
      ),
    ).toEqual({
      running: true,
      liveness: "running",
    });
  });

  test("reused pid (start epoch mismatch) → unknown, not live", () => {
    expect(
      observeAttemptLiveness(
        "RUNNING",
        PID,
        identity({ startEpoch: () => 100.89 }),
      ),
    ).toEqual({
      running: false,
      liveness: "unknown",
    });
  });

  test("unreadable start epoch → unknown, not live", () => {
    expect(
      observeAttemptLiveness(
        "RUNNING",
        PID,
        identity({ startEpoch: () => null }),
      ),
    ).toEqual({
      running: false,
      liveness: "unknown",
    });
  });

  test("this process's own pid is proven live by the real probe", () => {
    const started = processStartEpoch(process.pid);
    if (started == null) return;
    expect(
      observeAttemptLiveness("RUNNING", process.pid, {
        attemptHost: "Mac",
        attemptStartEpoch: started,
        currentHost: "Mac.local",
      }),
    ).toEqual({
      running: true,
      liveness: "running",
    });
  });

  test("builder pid dead, attempt in review via ACCEPTED result → alive", () => {
    const dead = deadPid();
    expect(
      observeAttemptLiveness("RUNNING", dead, {
        attemptHost: HOST,
        attemptStartEpoch: EPOCH,
        currentHost: HOST,
        declaredAccepted: true,
        isAlive: () => false,
      }),
    ).toEqual({
      running: true,
      liveness: "running",
    });
  });

  test("builder pid dead, attempt in review via review_dispatches → alive", () => {
    expect(
      observeAttemptLiveness("RUNNING", PID, {
        attemptHost: HOST,
        attemptStartEpoch: EPOCH,
        currentHost: HOST,
        reviewDispatches: [{ dispatch_no: 1 }],
        isAlive: () => false,
      }),
    ).toEqual({
      running: true,
      liveness: "running",
    });
  });

  test("empty review_dispatches is not the review window", () => {
    expect(
      observeAttemptLiveness(
        "RUNNING",
        PID,
        identity({ reviewDispatches: [], isAlive: () => false }),
      ),
    ).toEqual({
      running: false,
      liveness: "stale",
    });
  });

  test("review window does not apply once the attempt is closed", () => {
    expect(
      observeAttemptLiveness("VERIFIED", PID, {
        declaredAccepted: true,
        reviewDispatches: [{ dispatch_no: 1 }],
        isAlive: () => false,
      }),
    ).toEqual({
      running: false,
      liveness: "not_running",
    });
  });

  test("any non-RUNNING state → not running", () => {
    for (const state of ["CANCELLED", "MERGED", "FAILED", "READY", "BLOCKED", "VERIFIED"]) {
      expect(observeAttemptLiveness(state, PID, identity())).toEqual({
        running: false,
        liveness: "not_running",
      });
      expect(observeAttemptLiveness(state, null)).toEqual({
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
