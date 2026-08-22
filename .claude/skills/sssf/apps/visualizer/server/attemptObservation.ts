/**
 * Read-side attempt liveness. Not a lifecycle transition.
 *
 * Three answers, mirroring `lifecycle.attempt_liveness`:
 *   proven live  — `running`
 *   proven dead  — `stale`
 *   unprovable   — `unknown` / `not_recorded`
 *
 * A pid is only identity on the host that issued it, and only when its
 * start epoch still matches. Missing host/epoch (pre-#128 row), a foreign
 * host, or a reused pid is unknown — never dead, never live.
 *
 * The builder pid exits when the attempt enters review. That is expected.
 * RUNNING + an ACCEPTED result (or a stored `review_dispatches` row) is
 * the review window, and that phase reads alive without consulting the
 * builder pid. §1.2: this function observes; it never writes.
 */
import { hostname } from "node:os";
import { readFileSync, existsSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { createRequire } from "node:module";

const nodeRequire = createRequire(import.meta.url);

export type AttemptLiveness =
  | "running"
  | "stale"
  | "not_recorded"
  | "not_running"
  | "unknown";

/** Same key `retry_policy.REVIEW_DISPATCH_KEY` writes onto extra_json. */
export const REVIEW_DISPATCH_KEY = "review_dispatches";

export type ObserveAttemptLivenessInput = {
  state: string;
  pid: number | null;
  attemptHost?: string | null;
  attemptStartEpoch?: number | null;
  currentHost?: string | null;
  /** extra_json.review_dispatches — typed stall/re-dispatch records. */
  reviewDispatches?: unknown;
  /** results.adjudication === ACCEPTED for this attempt. */
  declaredAccepted?: boolean;
  isAlive?: (pid: number) => boolean;
  startEpoch?: (pid: number) => number | null;
};

export type AttemptLivenessObservation = {
  running: boolean;
  liveness: AttemptLiveness;
};

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

/**
 * Wall-clock start of `pid`, or null if it cannot be said.
 *
 * Same two platforms as `watchdog.process_start_epoch`: Linux `/proc` and
 * Darwin `proc_pidinfo`. Anywhere else — or any probe failure — is unknown,
 * never a coarse `ps lstart` guess. A reused pid in the same second must
 * not pass for the original.
 */
export function processStartEpoch(pid: number): number | null {
  if (!Number.isInteger(pid) || pid <= 0) return null;
  const linux = linuxProcessStartEpoch(pid);
  if (linux !== undefined) return linux;
  if (process.platform === "darwin") return darwinProcessStartEpoch(pid);
  return null;
}

function linuxProcessStartEpoch(pid: number): number | null | undefined {
  const statPath = `/proc/${pid}/stat`;
  if (!existsSync(statPath)) {
    return existsSync("/proc") ? null : undefined;
  }
  try {
    const body = readFileSync(statPath, "utf8").split(")", 1)[1];
    if (body == null) return null;
    const fields = body.split(/\s+/).filter(Boolean);
    const startTicks = Number(fields[19]);
    if (!Number.isFinite(startTicks)) return null;
    let boot: number | null = null;
    for (const line of readFileSync("/proc/stat", "utf8").split("\n")) {
      if (line.startsWith("btime ")) {
        boot = Number(line.split(/\s+/)[1]);
        break;
      }
    }
    const hz = linuxClkTck();
    if (boot == null || !Number.isFinite(boot) || hz <= 0) return null;
    return boot + startTicks / hz;
  } catch {
    return null;
  }
}

let cachedClkTck: number | null = null;

function linuxClkTck(): number {
  if (cachedClkTck != null) return cachedClkTck;
  const child = spawnSync("getconf", ["CLK_TCK"], { encoding: "utf8" });
  const n = Number((child.stdout ?? "").trim());
  cachedClkTck = Number.isFinite(n) && n > 0 ? n : 100;
  return cachedClkTck;
}

// proc_bsdinfo through pbi_start_tvusec. Matches adw_modules/watchdog.py.
const DARWIN_BSDINFO_SIZE = 136;
const DARWIN_PBI_PID_OFFSET = 12;
const DARWIN_START_SEC_OFFSET = 120;
const DARWIN_START_USEC_OFFSET = 128;
const PROC_PIDTBSDINFO = 3;

type DarwinProcPidInfo = (
  pid: number,
  flavor: number,
  arg: bigint,
  buffer: Uint8Array,
  size: number,
) => number;

let darwinProcPidInfo: DarwinProcPidInfo | null | undefined;

function loadDarwinProcPidInfo(): DarwinProcPidInfo | null {
  if (darwinProcPidInfo !== undefined) return darwinProcPidInfo;
  try {
    // bun:ffi is the Darwin probe. Failure is unknown, not a guess.
    // eslint-disable-next-line @typescript-eslint/no-require-imports
    const { dlopen, FFIType } = nodeRequire("bun:ffi") as {
      dlopen: (
        path: string,
        symbols: Record<string, { args: unknown[]; returns: unknown }>,
      ) => { symbols: { proc_pidinfo: DarwinProcPidInfo } };
      FFIType: { i32: unknown; u64: unknown; ptr: unknown };
    };
    const lib = dlopen("/usr/lib/libSystem.B.dylib", {
      proc_pidinfo: {
        args: [FFIType.i32, FFIType.i32, FFIType.u64, FFIType.ptr, FFIType.i32],
        returns: FFIType.i32,
      },
    });
    darwinProcPidInfo = lib.symbols.proc_pidinfo;
  } catch {
    darwinProcPidInfo = null;
  }
  return darwinProcPidInfo;
}

function darwinProcessStartEpoch(pid: number): number | null {
  const procPidInfo = loadDarwinProcPidInfo();
  if (procPidInfo == null) return null;
  const buffer = new Uint8Array(DARWIN_BSDINFO_SIZE);
  let got: number;
  try {
    got = procPidInfo(pid, PROC_PIDTBSDINFO, 0n, buffer, DARWIN_BSDINFO_SIZE);
  } catch {
    return null;
  }
  if (got !== DARWIN_BSDINFO_SIZE) return null;
  const view = new DataView(buffer.buffer, buffer.byteOffset, buffer.byteLength);
  if (view.getUint32(DARWIN_PBI_PID_OFFSET, true) !== pid) return null;
  const sec = Number(view.getBigUint64(DARWIN_START_SEC_OFFSET, true));
  const usec = Number(view.getBigUint64(DARWIN_START_USEC_OFFSET, true));
  if (!Number.isFinite(sec) || !Number.isFinite(usec)) return null;
  return sec + usec / 1_000_000;
}

export function inReviewWindow(
  state: string,
  reviewDispatches: unknown = undefined,
  declaredAccepted = false,
): boolean {
  if (state !== "RUNNING") return false;
  if (declaredAccepted) return true;
  return Array.isArray(reviewDispatches) && reviewDispatches.length > 0;
}

export function observeAttemptLiveness(
  state: string,
  pid: number | null,
  options: Omit<ObserveAttemptLivenessInput, "state" | "pid"> = {},
): AttemptLivenessObservation {
  if (state !== "RUNNING") {
    return { running: false, liveness: "not_running" };
  }
  if (inReviewWindow(state, options.reviewDispatches, options.declaredAccepted === true)) {
    return { running: true, liveness: "running" };
  }
  if (pid == null) {
    return { running: false, liveness: "not_recorded" };
  }
  if (!Number.isInteger(pid) || pid <= 0) {
    return { running: false, liveness: "unknown" };
  }
  const recordedHost = options.attemptHost ?? "";
  const recordedEpoch = options.attemptStartEpoch;
  if (!recordedHost || recordedEpoch == null) {
    return { running: false, liveness: "unknown" };
  }
  const current = options.currentHost ?? currentSchedulerHost();
  if (!sameSchedulerHost(recordedHost, current)) {
    return { running: false, liveness: "unknown" };
  }
  const isAlive = options.isAlive ?? pidIsAlive;
  if (!isAlive(pid)) {
    return { running: false, liveness: "stale" };
  }
  const startEpoch = options.startEpoch ?? processStartEpoch;
  const started = startEpoch(pid);
  if (started == null || started !== recordedEpoch) {
    return { running: false, liveness: "unknown" };
  }
  return { running: true, liveness: "running" };
}
