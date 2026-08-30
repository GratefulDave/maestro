/**
 * Factory visualizer server — JSON API over whichever run databases it is
 * pointed at, plus the built UI when ./dist exists. Reads are read-only; the
 * single write is POST /api/sessions/:adw_id/archive, which sets one review
 * flag on a tracer row.
 *
 * Three schemas are served, because three runtimes write different ledgers:
 * the SSSF tracer's sessions/phases/events, Maestro's legacy DAG store
 * (runs/dag_nodes/node_lifecycle/attempts), and the artifact-factory ledger
 * (runs/dag_lanes/lane_state/artifacts/transitions). Each database is probed
 * for the tables it actually has. See server/sources.ts.
 *
 * There is no ingest endpoint and no websocket. The data path is
 * agents → sqlite → web ui, and the UI gets there by polling.
 *
 *   bun run server/index.ts                       # discover both under cwd
 *   bun run server/index.ts --db /path/sssf.db --db /path/lifecycle.sqlite3
 *   MAESTRO_DB=/path/lifecycle.sqlite3 PORT=4600 bun run server/index.ts
 */
import { existsSync, statSync } from "node:fs";
import { join, resolve, sep } from "node:path";
import type { SssfDb } from "./db.ts";
import { resolveSources, type Source } from "./sources.ts";
import type { AgentPrompts, ApiError, HealthResponse } from "../shared/types.ts";

const PORT = Number(process.env.PORT ?? 4600);
const DIST_DIR = resolve(import.meta.dir, "..", "dist");

let sources: Source[];
try {
  sources = resolveSources();
} catch (error) {
  console.error(`[sssf] ${(error as Error).message}`);
  process.exit(1);
}
if (sources.length === 0) {
  console.error(
    "[sssf] no run database found.\n" +
      "Point the visualizer at one: --db <path> (repeatable), SSSF_DB=<path>, " +
      "MAESTRO_DB=<path>, or run it from a repo containing adws/adw_data/sssf.db " +
      "or adws/maestro.config.yaml",
  );
  process.exit(1);
}

/**
 * The tracer source the legacy /api/sessions routes speak to.
 *
 * Those routes predate the source registry and are addressed without a source
 * id, so they bind to the first sssf database. A process serving only Maestro
 * has none, and they 404 rather than throwing.
 */
const db: SssfDb | undefined = sources.find((source) => source.kind === "sssf")?.sssf;

function json(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": "no-store",
    },
  });
}

function notFound(message: string): Response {
  return json({ error: message } satisfies ApiError, 404);
}

/** Guard every handler so a malformed query can't take the server down mid-run. */
function safely(
  handler: (req: Request) => Response | Promise<Response>,
): (req: Request) => Promise<Response> {
  return async (req) => {
    try {
      return await handler(req);
    } catch (error) {
      console.error(`[sssf] ${req.method} ${new URL(req.url).pathname}:`, error);
      return json({ error: (error as Error).message } satisfies ApiError, 500);
    }
  };
}

/**
 * adw_ids and agent names are path segments on disk, so anything that isn't a
 * plain identifier is rejected outright rather than sanitized into something
 * that might still escape the sessions directory.
 */
const SAFE_SEGMENT = /^[A-Za-z0-9._-]+$/;

function isSafeSegment(value: string): boolean {
  return SAFE_SEGMENT.test(value) && value !== "." && value !== "..";
}

function param(req: Request, key: string): string {
  return decodeURIComponent(
    (req as Request & { params: Record<string, string> }).params[key] ?? "",
  );
}

function intQuery(req: Request, key: string, fallback: number): number {
  const raw = new URL(req.url).searchParams.get(key);
  if (raw === null || raw.trim() === "") return fallback;
  const parsed = Number.parseInt(raw, 10);
  return Number.isFinite(parsed) ? parsed : fallback;
}

/** Serve the built SPA if it has been built; otherwise point at the dev server. */
async function serveStatic(req: Request): Promise<Response> {
  const { pathname } = new URL(req.url);

  if (!existsSync(DIST_DIR)) {
    return new Response(
      `SSSF visualizer API is running on :${PORT}.\n\n` +
        `No ./dist build found. Run "bun run dev" for the Vite dev server ` +
        `(it proxies /api here), or "bun run build" to serve the UI from this process.\n`,
      { status: 200, headers: { "content-type": "text/plain; charset=utf-8" } },
    );
  }

  // Reject traversal before touching the filesystem.
  const candidate = resolve(join(DIST_DIR, pathname));
  if (candidate === DIST_DIR || candidate.startsWith(DIST_DIR + "/")) {
    if (existsSync(candidate) && statSync(candidate).isFile()) {
      return new Response(Bun.file(candidate));
    }
  }

  // SPA fallback: breadcrumb routes are client-side.
  const indexHtml = join(DIST_DIR, "index.html");
  if (existsSync(indexHtml)) {
    return new Response(Bun.file(indexHtml), {
      headers: { "content-type": "text/html; charset=utf-8" },
    });
  }
  return notFound("not found");
}

/** The tracer routes are only meaningful when a tracer database is loaded. */
function withSssf(handler: (sssf: SssfDb, req: Request) => Response): (req: Request) => Response {
  return (req) => (db ? handler(db, req) : notFound("no sssf database is loaded"));
}

/** Look up a registered source by the id the UI carries in its route. */
function sourceFor(req: Request): Source | undefined {
  return sources.find((source) => source.id === param(req, "source_id"));
}

const server = Bun.serve({
  port: PORT,
  routes: {
    "/api/health": safely(
      () =>
        json({
          ok: true,
          db: sources[0]!.path,
          journal_mode: sources[0]!.info().journal_mode,
          sessions: db?.sessionCount() ?? 0,
          sources: sources.map((source) => source.info()),
        } satisfies HealthResponse),
    ),

    /** Every database this process serves, and which view each one needs. */
    "/api/sources": safely(() => json(sources.map((source) => source.info()))),

    /** Run index for a maestro or artifact-factory ledger, newest first. */
    "/api/sources/:source_id/runs": safely((req) => {
      const source = sourceFor(req);
      const ledger = source?.maestro ?? source?.artifactFactory;
      if (!ledger) return notFound(`no run source ${param(req, "source_id")}`);
      return json(ledger.runs());
    }),

    /** One run whole — mapped onto the existing reporting types. */
    "/api/sources/:source_id/runs/:run_id": safely((req) => {
      const source = sourceFor(req);
      const ledger = source?.maestro ?? source?.artifactFactory;
      if (!ledger) return notFound(`no run source ${param(req, "source_id")}`);
      const detail = ledger.run(param(req, "run_id"));
      return detail ? json(detail) : notFound(`no run ${param(req, "run_id")}`);
    }),

    "/api/sessions": safely(
      withSssf((sssf, req) => json(sssf.sessions(intQuery(req, "limit", 200)))),
    ),

    "/api/sessions/:adw_id": safely(
      withSssf((sssf, req) => {
        const detail = sssf.sessionDetail(param(req, "adw_id"));
        return detail ? json(detail) : notFound(`no session ${param(req, "adw_id")}`);
      }),
    ),

    // The one write. Archiving is review triage — it belongs to the reader, not
    // to the run — so it never touches anything a tracer wrote.
    "/api/sessions/:adw_id/archive": {
      POST: safely(async (req) => {
        if (!db) return notFound("no sssf database is loaded");
        const adwId = param(req, "adw_id");
        if (!isSafeSegment(adwId)) {
          return json({ error: "invalid adw_id" } satisfies ApiError, 400);
        }
        const body = (await req.json().catch(() => ({}))) as { archived?: unknown };
        const archived = body.archived === undefined ? true : Boolean(body.archived);
        return db.setArchived(adwId, archived)
          ? json({ adw_id: adwId, archived })
          : notFound(`no session ${adwId}`);
      }),
    },

    "/api/sessions/:adw_id/events": safely(
      withSssf((sssf, req) =>
        json(
          sssf.events(
            param(req, "adw_id"),
            intQuery(req, "after", 0),
            intQuery(req, "limit", 500),
          ),
        ),
      ),
    ),

    "/api/sessions/:adw_id/envelopes": safely(
      withSssf((sssf, req) => json(sssf.envelopes(param(req, "adw_id")))),
    ),

    "/api/sessions/:adw_id/gates": safely(
      withSssf((sssf, req) => json(sssf.gates(param(req, "adw_id")))),
    ),

    // The exact prompts an agent was sent, read from the session dir. Files are
    // the raw record; the db has no copy of them.
    "/api/sessions/:adw_id/agents/:agent/prompts": safely(async (req) => {
      if (!db) return notFound("no sssf database is loaded");
      const adwId = param(req, "adw_id");
      const agent = param(req, "agent");
      if (!isSafeSegment(adwId) || !isSafeSegment(agent)) {
        return json({ error: "invalid adw_id or agent" } satisfies ApiError, 400);
      }
      if (!db.session(adwId)) return notFound(`no session ${adwId}`);

      const dir = resolve(db.sessionsDir, adwId, agent, "prompts");
      // Defense in depth: the segment check already forbids traversal.
      if (dir !== db.sessionsDir && !dir.startsWith(db.sessionsDir + sep)) {
        return json({ error: "invalid path" } satisfies ApiError, 400);
      }

      // A prompt file is absent whenever the agent never ran in this session —
      // a normal state, so it reads as null rather than an error.
      const read = async (name: string): Promise<string | null> => {
        const file = Bun.file(join(dir, `${name}.md`));
        return (await file.exists()) ? await file.text() : null;
      };
      return json({
        system: await read("system"),
        user: await read("user"),
      } satisfies AgentPrompts);
    }),
  },

  fetch(req) {
    const { pathname } = new URL(req.url);
    if (pathname.startsWith("/api/")) return notFound(`no route ${pathname}`);
    return serveStatic(req);
  },
});

console.log(`[sssf] visualizer api  http://localhost:${server.port}`);
for (const source of sources) {
  const info = source.info();
  console.log(
    `[sssf] ${info.kind.padEnd(16)} ${info.path}  ` +
      `[journal_mode=${info.journal_mode}, ${info.count} ${
        info.kind === "sssf" ? "sessions" : "runs"
      }]`,
  );
}
console.log(
  existsSync(DIST_DIR)
    ? `[sssf] serving ui from  ${DIST_DIR}`
    : `[sssf] no ./dist — use "bun run dev" for the Vite dev server on :4601`,
);

process.on("SIGINT", () => {
  for (const source of sources) source.close();
  process.exit(0);
});
