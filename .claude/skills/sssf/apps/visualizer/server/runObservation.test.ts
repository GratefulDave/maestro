import { describe, expect, test } from "bun:test";
import { displayRunState, observeRunLiveness } from "./runObservation.ts";

const HOST = "test-host";
const EPOCH = 100.5;
const PID = 4242;

function identity(
  overrides: Partial<Parameters<typeof observeRunLiveness>[0]> = {},
) {
  return {
    liveState: "RUNNING",
    schedulerPid: PID,
    schedulerHost: HOST,
    schedulerStartEpoch: EPOCH,
    currentHost: HOST,
    isAlive: () => true,
    startEpoch: () => EPOCH,
    ...overrides,
  };
}

function trackingAlive(calls: number[]) {
  return (pid: number) => {
    calls.push(pid);
    return true;
  };
}

describe("observeRunLiveness", () => {
  test("live scheduler, host and epoch match → running", () => {
    expect(observeRunLiveness(identity())).toBe("running");
  });

  test("dead pid, host matches, epoch recorded, no terminal live state → abandoned", () => {
    const epochCalls: number[] = [];
    expect(
      observeRunLiveness(
        identity({
          isAlive: () => false,
          startEpoch: (pid) => {
            epochCalls.push(pid);
            return EPOCH;
          },
        }),
      ),
    ).toBe("abandoned");
    expect(epochCalls).toEqual([]);
  });

  test("absent pid (old ledger / missing columns) → unknown, not abandoned", () => {
    expect(observeRunLiveness(identity({ schedulerPid: null }))).toBe("unknown");
  });

  test("pid present but host or epoch missing → unknown, not probed", () => {
    const aliveCalls: number[] = [];
    const isAlive = trackingAlive(aliveCalls);
    expect(
      observeRunLiveness(identity({ isAlive, schedulerHost: null })),
    ).toBe("unknown");
    expect(
      observeRunLiveness(identity({ isAlive, schedulerStartEpoch: null })),
    ).toBe("unknown");
    expect(aliveCalls).toEqual([]);
  });

  test("foreign host → unknown, not abandoned, not probed", () => {
    const aliveCalls: number[] = [];
    expect(
      observeRunLiveness(
        identity({
          schedulerHost: "other-box",
          isAlive: trackingAlive(aliveCalls),
        }),
      ),
    ).toBe("unknown");
    expect(aliveCalls).toEqual([]);
  });

  test("epoch mismatch → unknown, not abandoned", () => {
    expect(observeRunLiveness(identity({ startEpoch: () => EPOCH + 1 }))).toBe(
      "unknown",
    );
    expect(observeRunLiveness(identity({ startEpoch: () => null }))).toBe(
      "unknown",
    );
  });

  test("FQDN vs short label on this host still runs", () => {
    expect(
      observeRunLiveness(
        identity({ schedulerHost: "Mac.attlocal.net", currentHost: "Mac" }),
      ),
    ).toBe("running");
  });

  test("pid <= 0 → unknown, never probed", () => {
    const aliveCalls: number[] = [];
    const isAlive = trackingAlive(aliveCalls);
    expect(observeRunLiveness(identity({ isAlive, schedulerPid: 0 }))).toBe(
      "unknown",
    );
    expect(observeRunLiveness(identity({ isAlive, schedulerPid: -1 }))).toBe(
      "unknown",
    );
    expect(aliveCalls).toEqual([]);
  });

  test("already-terminal live state → not_running, never probed", () => {
    const aliveCalls: number[] = [];
    expect(
      observeRunLiveness(
        identity({
          liveState: "MERGED",
          isAlive: (pid) => {
            aliveCalls.push(pid);
            return false;
          },
        }),
      ),
    ).toBe("not_running");
    expect(aliveCalls).toEqual([]);
  });
});

describe("displayRunState", () => {
  test("abandoned overlays ABANDONED; unknown keeps the live-looking state", () => {
    expect(displayRunState("RUNNING", "abandoned")).toBe("ABANDONED");
    expect(displayRunState("RUNNING", "unknown")).toBe("RUNNING");
    expect(displayRunState("RUNNING", "running")).toBe("RUNNING");
    expect(displayRunState("MERGED", "not_running")).toBe("MERGED");
  });
});
