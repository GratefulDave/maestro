/**
 * The dashboard's source registry.
 *
 * One dashboard, many factories. An "ADW" is not one schema: the SSSF tracer
 * writes a linear session/phase/event ledger, Maestro's DAG runtime writes
 * runs/nodes/attempts, and the next one will write a third thing. Rather than
 * forcing every runtime through one table shape — which would have to drop
 * whichever facts it does not share, starting with Maestro's dependency edges —
 * each runtime gets a reader, and this module decides which reader a given
 * database needs by looking at the tables that are actually in it.
 *
 * Adding a new ADW is therefore: write a reader, add a `SourceKind`, add a
 * probe row to `PROBES`, and add a view keyed on that kind. Nothing existing
 * changes, and no runtime is asked to write a second schema.
 *
 * Every source is opened read-only. The one write in this process remains
 * SssfDb's `setArchived`, which is review triage on a tracer row.
 */
import { Database } from "bun:sqlite";
import { existsSync, readFileSync } from "node:fs";
import { basename, isAbsolute, resolve } from "node:path";
import { SssfDb } from "./db.ts";
import { MAESTRO_TABLES, MaestroDb, discoverMaestroLedger, openLedgerReadonly } from "./maestroDb.ts";
import type { SourceInfo, SourceKind } from "../shared/types.ts";

const DEFAULT_SSSF_RELATIVE = "adws/adw_data/sssf.db";

/**
 * Where Maestro records the installations it has run.
 *
 * The dashboard is one process watching several factories at once, and each
 * factory keeps its ledger beside its own repository, in a directory the
 * dashboard has no way to guess. Requiring `MAESTRO_DB` per factory made the
 * operator restate, every session, something Maestro already knew — and it
 * caps the dashboard at whatever fits on one command line.
 *
 * So Maestro writes it down. Each configured run records its ledger here, and
 * the dashboard opens every entry. Five factories become five tabs with no
 * arguments at all.
 */
const REGISTRY_RELATIVE = ".maestro/registry.json";

type RegistryEntry = { db: string; plansDir: string | null };

/** Installations Maestro has recorded, newest first, unreadable ones dropped. */
export function registeredInstallations(
  home = process.env.MAESTRO_REGISTRY ?? null,
): RegistryEntry[] {
  const path =
    home ?? resolve(process.env.HOME ?? process.env.USERPROFILE ?? ".", REGISTRY_RELATIVE);
  if (!existsSync(path)) return [];
  let parsed: unknown;
  try {
    parsed = JSON.parse(readFileSync(path, "utf8"));
  } catch (error) {
    // A half-written registry costs the operator nothing but auto-discovery.
    console.warn(`[sssf] ignoring ${path}: ${(error as Error).message}`);
    return [];
  }
  const raw = (parsed as { installations?: unknown })?.installations;
  if (!Array.isArray(raw)) return [];
  const entries: RegistryEntry[] = [];
  for (const item of raw) {
    const db = (item as { database?: unknown })?.database;
    if (typeof db !== "string" || !db) continue;
    const plans = (item as { plans_dir?: unknown })?.plans_dir;
    entries.push({ db, plansDir: typeof plans === "string" && plans ? plans : null });
  }
  return entries;
}

/** The tables that identify each schema. First full match wins. */
const PROBES: { kind: SourceKind; tables: string[] }[] = [
  { kind: "maestro", tables: MAESTRO_TABLES },
  { kind: "sssf", tables: ["sessions", "phases", "events"] },
];

/**
 * Which schema a database file holds.
 *
 * Probed rather than inferred from the filename, because both runtimes let an
 * operator relocate their database and a path is not a contract. Opened with
 * the lifecycle reader's tolerant readonly open so a cleanly-closed WAL
 * database — every finished Maestro run — is still identifiable.
 */
export function probeKind(path: string): SourceKind | null {
  let db: Database;
  try {
    db = openLedgerReadonly(path).db;
  } catch (error) {
    console.warn(`[sssf] cannot read ${path}: ${(error as Error).message}`);
    return null;
  }
  try {
    const names = new Set(
      db
        .query<{ name: string }, []>("SELECT name FROM sqlite_master WHERE type='table'")
        .all()
        .map((row) => row.name),
    );
    return PROBES.find((probe) => probe.tables.every((t) => names.has(t)))?.kind ?? null;
  } catch (error) {
    // A file that is not a database at all, or one truncated mid-write: the
    // registry drops it with a warning rather than taking the server down.
    console.warn(`[sssf] cannot read ${path}: ${(error as Error).message}`);
    return null;
  } finally {
    db.close();
  }
}

export interface Source {
  readonly id: string;
  readonly kind: SourceKind;
  readonly path: string;
  readonly label: string;
  readonly sssf?: SssfDb;
  readonly maestro?: MaestroDb;
  info(): SourceInfo;
  close(): void;
}

function makeSource(
  kind: SourceKind,
  path: string,
  plansDir: string | null,
  taken: Set<string>,
): Source {
  // The id is what the UI puts in its hash route, so it has to be stable and
  // URL-safe. The database's own directory name is both, and it is what an
  // operator recognises: "lexgenius", not "source-2".
  const base = basename(resolve(path, "..")).replace(/[^A-Za-z0-9._-]/g, "-") || kind;
  let id = `${kind}:${base}`;
  let n = 2;
  while (taken.has(id)) id = `${kind}:${base}-${n++}`;
  taken.add(id);

  if (kind === "sssf") {
    const sssf = new SssfDb(path);
    return {
      id,
      kind,
      path,
      label: base,
      sssf,
      info: () => ({
        id,
        kind,
        path,
        label: base,
        journal_mode: sssf.journalMode,
        count: sssf.sessionCount(),
      }),
      close: () => sssf.close(),
    };
  }
  const maestro = new MaestroDb(path, plansDir);
  return {
    id,
    kind,
    path,
    label: base,
    maestro,
    info: () => ({
      id,
      kind,
      path,
      label: base,
      journal_mode: maestro.journalMode,
      count: maestro.runCount(),
    }),
    close: () => maestro.close(),
  };
}

/**
 * Every database the server was pointed at, in the order it was given.
 *
 * `--db` may be repeated, and `SSSF_DB` / `MAESTRO_DB` may each name one, so a
 * single process can serve a repo's tracer ledger and its Maestro ledger side
 * by side. With nothing supplied, the two conventional locations under the
 * current repo are auto-discovered — the visualizer is run from the target
 * repo, the same way every other verb is.
 */
export function resolveSources(argv: string[] = Bun.argv, cwd = process.cwd()): Source[] {
  const requested: { path: string; plansDir: string | null }[] = [];
  const add = (raw: string | undefined, plansDir: string | null = null) => {
    if (!raw) return;
    const path = isAbsolute(raw) ? raw : resolve(cwd, raw);
    if (!requested.some((item) => item.path === path)) requested.push({ path, plansDir });
  };

  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i] ?? "";
    if (arg === "--db") add(argv[i + 1]);
    else if (arg.startsWith("--db=")) add(arg.slice("--db=".length));
  }
  add(process.env.SSSF_DB);
  add(process.env.MAESTRO_DB, process.env.MAESTRO_PLANS ?? null);

  if (requested.length === 0) {
    add(DEFAULT_SSSF_RELATIVE);
    const discovered = discoverMaestroLedger(cwd);
    if (discovered) add(discovered.db, discovered.plansDir);
    // The repo we are standing in comes first so it stays the landing view;
    // every other factory Maestro has run is added behind it as a tab.
    for (const entry of registeredInstallations()) add(entry.db, entry.plansDir);
  } else {
    // An explicitly-named Maestro ledger still gets its plan names when the
    // repo it belongs to is the one we are standing in.
    const discovered = discoverMaestroLedger(cwd);
    if (discovered) {
      for (const item of requested) {
        if (item.path === discovered.db && item.plansDir === null) {
          item.plansDir = discovered.plansDir;
        }
      }
    }
  }

  // A plans directory named once applies to every Maestro ledger that has not
  // been given one — otherwise `--db <ledger>` silently loses plan names, since
  // the ledger stores digests and only the plan files can name them.
  for (const item of requested) {
    item.plansDir ??= process.env.MAESTRO_PLANS ?? null;
  }

  const taken = new Set<string>();
  const sources: Source[] = [];
  for (const item of requested) {
    if (!existsSync(item.path)) {
      console.warn(`[sssf] skipping ${item.path} — no such file`);
      continue;
    }
    const kind = probeKind(item.path);
    if (kind === null) {
      console.warn(`[sssf] skipping ${item.path} — not an sssf or maestro database`);
      continue;
    }
    try {
      sources.push(makeSource(kind, item.path, item.plansDir, taken));
    } catch (error) {
      // One unreadable database must not cost the operator the others.
      console.warn(`[sssf] skipping ${item.path}: ${(error as Error).message}`);
    }
  }
  return sources;
}
