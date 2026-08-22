/**
 * Read-side pid liveness. Not a lifecycle transition.
 *
 * The ledger's attempts.state stays RUNNING when the scheduler never wrote a
 * terminal row. A viewer may report that disagreement; it must not resolve it.
 *
 * A fifth answer exists because a pid is only meaningful on the host that
 * issued it. Matching the runtime's `scheduler_liveness`: unknown is its own
 * value, never treated as dead, and never treated as running.
 */
import { hostname } from "node:os";

export type AttemptLiveness =
  | "running"
  | "stale"
  | "not_recorded"
  | "not_running"
  | "unknown";

/** First DNS label, case-insensitive. An FQDN's DHCP suffix is not identity. */
export function hostIdentity(name: string): string {
  const label = name.trim().split(".", 1)[0] ?? "";
  return label.toLowerCase();
}

export function sameSchedulerHost(recorded: string, current: string): boolean {
  const left = hostIdentity(recorded);
  const right = hostIdentity(current);
  return Boolean(left) && left === right;
}

export function currentSchedulerHost(): string {
  return hostname();
}

export function pidIsAlive(pid: number): boolean {
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    if (typeof error === "object" && error !== null && "code" in error) {
      // EPERM: process exists, we just cannot signal it.
      return error.code === "EPERM";
    }
    return false;
  }
}

export function observeAttemptLiveness(
  state: string,
  pid: number | null,
  schedulerHost: string | null = null,
  currentHost: string | null = null,
): { running: boolean; liveness: AttemptLiveness } {
  if (state !== "RUNNING") {
    return { running: false, liveness: "not_running" };
  }
  if (pid == null) {
    return { running: false, liveness: "not_recorded" };
  }
  if (!Number.isInteger(pid) || pid <= 0) {
    return { running: false, liveness: "unknown" };
  }
  const recorded = schedulerHost ?? "";
  const current = currentHost ?? currentSchedulerHost();
  if (recorded && !sameSchedulerHost(recorded, current)) {
    return { running: false, liveness: "unknown" };
  }
  const alive = pidIsAlive(pid);
  return alive
    ? { running: true, liveness: "running" }
    : { running: false, liveness: "stale" };
}
