/**
 * Read-side vendor/model identity. Attempts have no vendor or model column.
 * Observed truth is the session jsonl `model_change` record. Declared truth
 * is the deployment maestro.config.yaml role block. Neither is written back.
 */
import { closeSync, existsSync, fstatSync, openSync, readFileSync, readSync } from "node:fs";
import { basename, dirname, join } from "node:path";

export type IdentitySource = "observed" | "observed_head" | "declared" | "not_recorded";
export type AgentRole = "execution" | "reviewer" | "author" | "tester";

export interface AttemptIdentity {
  model: string | null;
  vendor: string | null;
  model_source: IdentitySource;
  vendor_source: IdentitySource;
  declared_config_path: string | null;
}

export interface DeclaredRole {
  model: string | null;
  vendor: string | null;
}

export type DeclaredRoles = Partial<Record<AgentRole, DeclaredRole>>;

export interface DeclaredConfig {
  path: string | null;
  roles: DeclaredRoles;
}

export interface ObservedModel {
  model: string | null;
  vendor: string | null;
  window: "full" | "head_and_tail" | "head";
}

const NOT_RECORDED: AttemptIdentity = {
  model: null,
  vendor: null,
  model_source: "not_recorded",
  vendor_source: "not_recorded",
  declared_config_path: null,
};

const HEAD_BYTES = 64 * 1024;
const ROLES: AgentRole[] = ["execution", "reviewer", "author", "tester"];

/**
 * Typed `NodeKind` first: agent→execution, review→reviewer, tests→tester,
 * code→null. Id-prefix guessing only when kind is absent.
 */
export function roleForNode(nodeId: string, kind: string | null): AgentRole | null {
  const k = (kind ?? "").toLowerCase();
  if (k === "agent") return "execution";
  if (k === "review") return "reviewer";
  if (k === "tests") return "tester";
  if (k === "code") return null;
  if (k) return null;
  const id = nodeId.toLowerCase();
  if (id.startsWith("review")) return "reviewer";
  if (id.startsWith("author")) return "author";
  if (id.startsWith("test")) return "tester";
  if (id.startsWith("lane-")) return "execution";
  return null;
}

export function maestroConfigPathFromLedger(
  dbPath: string,
  plansDir: string | null,
): string | null {
  const candidates: string[] = [];
  if (plansDir) {
    candidates.push(join(dirname(plansDir), "maestro.config.yaml"));
    candidates.push(join(dirname(plansDir), "adws", "maestro.config.yaml"));
    candidates.push(join(dirname(dirname(plansDir)), "adws", "maestro.config.yaml"));
  }
  const ledgerDir = dirname(dbPath);
  const stateRoot = dirname(ledgerDir);
  const repoName = basename(ledgerDir);
  candidates.push(join(dirname(stateRoot), repoName, "adws", "maestro.config.yaml"));
  for (const candidate of candidates) {
    if (existsSync(candidate)) return candidate;
  }
  return null;
}

export function parseDeclaredRoles(text: string): DeclaredRoles {
  const roles: DeclaredRoles = {};
  let current: AgentRole | null = null;
  for (const raw of text.split(/\r?\n/)) {
    const top = raw.match(/^([A-Za-z_][\w-]*)\s*:/);
    if (top) {
      const name = top[1] as AgentRole;
      current = ROLES.includes(name) ? name : null;
      if (current && !roles[current]) roles[current] = { model: null, vendor: null };
      continue;
    }
    if (!current) continue;
    const field = raw.match(/^\s+(model|vendor)\s*:\s*(.+?)\s*$/);
    if (!field) continue;
    const value = field[2]!.replace(/\s+#.*$/, "").replace(/^["']|["']$/g, "").trim();
    if (!value) continue;
    const block = roles[current] ?? { model: null, vendor: null };
    if (field[1] === "model") block.model = value;
    else block.vendor = value;
    roles[current] = block;
  }
  return roles;
}

export function loadDeclaredRoles(
  dbPath: string,
  plansDir: string | null,
): DeclaredConfig {
  const config = maestroConfigPathFromLedger(dbPath, plansDir);
  if (!config) return { path: null, roles: {} };
  try {
    return { path: config, roles: parseDeclaredRoles(readFileSync(config, "utf8")) };
  } catch {
    return { path: config, roles: {} };
  }
}

function lastModelChange(text: string): { model: string | null; vendor: string | null; found: boolean } {
  let model: string | null = null;
  let vendor: string | null = null;
  let found = false;
  for (const line of text.split(/\r?\n/)) {
    if (!line.includes("model_change")) continue;
    try {
      const rec = JSON.parse(line) as Record<string, unknown>;
      if (rec.type !== "model_change") continue;
      found = true;
      if (typeof rec.model === "string" && rec.model) model = rec.model;
      if (typeof rec.vendor === "string" && rec.vendor) vendor = rec.vendor;
    } catch {
      // skip a truncated line at a window edge
    }
  }
  return { model, vendor, found };
}

export function readObservedModel(sessionPath: string): ObservedModel {
  const empty: ObservedModel = { model: null, vendor: null, window: "full" };
  if (!existsSync(sessionPath)) return empty;
  let fd: number | null = null;
  try {
    fd = openSync(sessionPath, "r");
    const size = fstatSync(fd).size;
    const head = Buffer.alloc(Math.min(HEAD_BYTES, size));
    const n = readSync(fd, head, 0, head.length, 0);
    const headParsed = lastModelChange(head.toString("utf8", 0, n));
    if (size <= HEAD_BYTES) {
      return { model: headParsed.model, vendor: headParsed.vendor, window: "full" };
    }
    const tailLen = Math.min(HEAD_BYTES, size);
    const tail = Buffer.alloc(tailLen);
    const tn = readSync(fd, tail, 0, tail.length, size - tailLen);
    const tailParsed = lastModelChange(tail.toString("utf8", 0, tn));
    if (tailParsed.found) {
      return { model: tailParsed.model, vendor: tailParsed.vendor, window: "head_and_tail" };
    }
    return { model: headParsed.model, vendor: headParsed.vendor, window: "head" };
  } catch {
    return empty;
  } finally {
    if (fd != null) closeSync(fd);
  }
}

export function resolveAttemptIdentity(
  sessionPath: string | null,
  declared: DeclaredRole | null | undefined,
  observedCache: Map<string, ObservedModel>,
  declaredConfigPath: string | null = null,
): AttemptIdentity {
  if (sessionPath) {
    let observed = observedCache.get(sessionPath);
    if (!observed) {
      observed = readObservedModel(sessionPath);
      observedCache.set(sessionPath, observed);
    }
    if (observed.model || observed.vendor) {
      const source = observed.window === "head" ? "observed_head" : "observed";
      return {
        model: observed.model,
        vendor: observed.vendor,
        model_source: observed.model ? source : "not_recorded",
        vendor_source: observed.vendor ? source : "not_recorded",
        declared_config_path: declaredConfigPath,
      };
    }
  }
  if (declared && (declared.model || declared.vendor)) {
    return {
      model: declared.model,
      vendor: declared.vendor,
      model_source: declared.model ? "declared" : "not_recorded",
      vendor_source: declared.vendor ? "declared" : "not_recorded",
      declared_config_path: declaredConfigPath,
    };
  }
  return { ...NOT_RECORDED, declared_config_path: declaredConfigPath };
}
