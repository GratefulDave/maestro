/**
 * Read-side observation of whether a run's scheduler is still there.
 *
 * Same reasoning #127 gave the attempt: three answers, never a bare boolean.
 * `running` / `abandoned` / `unknown` for an in-flight-looking run;
 * `not_running` when the node-derived live state is already terminal.
 * §1.2: this function observes; it never writes an outcome or drives a
 * transition. Collapsing `unknown` into `abandoned` is the conviction this
 * exists to prevent.
 */
import type { RunLiveness } from "../shared/types.ts";
import {
  currentSchedulerHost,
  pidIsAlive,
  processStartEpoch,
  sameSchedulerHost,
} from "./attemptObservation.ts";

export type ObserveRunLivenessInput = {
  liveState: string;
  schedulerPid: number | null;
  schedulerHost?: string | null;
  schedulerStartEpoch?: number | null;
  currentHost?: string | null;
  isAlive?: (pid: number) => boolean;
  startEpoch?: (pid: number) => number | null;
};

export function observeRunLiveness(
  input: ObserveRunLivenessInput,
): RunLiveness {
  if (!["RUNNING", "CANCELLING", "PENDING"].includes(input.liveState)) {
    return "not_running";
  }


  const pid = input.schedulerPid;
  if (pid == null) return "unknown";
  if (!Number.isInteger(pid) || pid <= 0) return "unknown";
  const recordedHost = input.schedulerHost ?? "";
  const recordedEpoch = input.schedulerStartEpoch;
  if (!recordedHost || recordedEpoch == null || !Number.isFinite(recordedEpoch)) {
    return "unknown";
  }
  const current = input.currentHost ?? currentSchedulerHost();
  if (!sameSchedulerHost(recordedHost, current)) return "unknown";
  const isAlive = input.isAlive ?? pidIsAlive;
  if (!isAlive(pid)) return "abandoned";
  const started = (input.startEpoch ?? processStartEpoch)(pid);
  if (started == null || started !== recordedEpoch) return "unknown";
  return "running";
}

/** Overlay only when the scheduler is proven gone. Unknown stays live-looking. */
export function displayRunState(
  liveState: string,
  liveness: RunLiveness,
): string {
  return liveness === "abandoned" ? "ABANDONED" : liveState;
}
