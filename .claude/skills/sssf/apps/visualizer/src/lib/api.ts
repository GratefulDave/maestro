import type {
  Envelope,
  EventRow,
  EventsPage,
  GateResult,
  HealthResponse,
  MaestroRunDetail,
  MaestroRunSummary,
  PromptsResponse,
  SessionDetail,
  SessionSummary,
  SourceInfo,
} from './types'

/**
 * Thrown by `getJson` on a non-2xx response, carrying the HTTP status so a
 * caller can tell "this entity is gone" (404) apart from "the server is
 * unreachable right now" — a poll loop that only sees `Error` cannot make
 * that distinction and ends up leaving a deleted entity's last-known state on
 * screen forever, which is indistinguishable from it still being live.
 */
export class ApiHttpError extends Error {
  constructor(
    readonly status: number,
    url: string,
  ) {
    super(`GET ${url} → ${status}`)
    this.name = 'ApiHttpError'
  }
}

async function getJson(url: string): Promise<unknown> {
  const res = await fetch(url)
  if (!res.ok) throw new ApiHttpError(res.status, url)
  return res.json()
}

export function fetchSessions(): Promise<SessionSummary[]> {
  return getJson('/api/sessions') as Promise<SessionSummary[]>
}

export async function fetchSession(adwId: string): Promise<SessionDetail> {
  const detail = (await getJson(`/api/sessions/${encodeURIComponent(adwId)}`)) as SessionDetail
  return {
    session: detail.session,
    usage: detail.usage ?? { read: 0, written: 0 },
    phases: detail.phases ?? [],
    agents: detail.agents ?? [],
  }
}

export async function fetchEvents(adwId: string, after: number, limit = 500): Promise<EventsPage> {
  const page = (await getJson(
    `/api/sessions/${encodeURIComponent(adwId)}/events?after=${after}&limit=${limit}`,
  )) as EventsPage | EventRow[]
  if (Array.isArray(page)) {
    const cursor = page.reduce((max, e) => Math.max(max, e.rowid), after)
    return { events: page, cursor, has_more: page.length === limit }
  }
  return { events: page.events ?? [], cursor: page.cursor ?? after, has_more: page.has_more ?? false }
}

/** Archive a run out of the review list (or restore it with archived=false). */
export async function archiveSession(adwId: string, archived = true): Promise<void> {
  const url = `/api/sessions/${encodeURIComponent(adwId)}/archive`
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ archived }),
  })
  if (!res.ok) throw new Error(`POST ${url} → ${res.status}`)
}

export function fetchHealth(): Promise<HealthResponse> {
  return getJson('/api/health') as Promise<HealthResponse>
}

// ── sources ──────────────────────────────────────────────────────────────────
// Which run databases the server is serving, and which schema each one holds.
// The UI picks a view from `kind`, so a new factory runtime becomes visible by
// adding a kind and a view — no change to the ones already here.

export function fetchSources(): Promise<SourceInfo[]> {
  return getJson('/api/sources') as Promise<SourceInfo[]>
}

export function fetchRuns(sourceId: string): Promise<MaestroRunSummary[]> {
  return getJson(`/api/sources/${encodeURIComponent(sourceId)}/runs`) as Promise<
    MaestroRunSummary[]
  >
}

export function fetchRun(sourceId: string, runId: string): Promise<MaestroRunDetail> {
  return getJson(
    `/api/sources/${encodeURIComponent(sourceId)}/runs/${encodeURIComponent(runId)}`,
  ) as Promise<MaestroRunDetail>
}

// PhaseDetail imports the prompts type from here alongside fetchPrompts.
export type { PromptsResponse }

export async function fetchPrompts(adwId: string, agent: string): Promise<PromptsResponse> {
  const res = await fetch(
    `/api/sessions/${encodeURIComponent(adwId)}/agents/${encodeURIComponent(agent)}/prompts`,
  )
  // Not recorded (or endpoint not deployed yet) renders as "no prompts", not an error.
  if (res.status === 404) return { system: null, user: null }
  if (!res.ok) throw new Error(`GET prompts → ${res.status}`)
  const data = (await res.json()) as Partial<PromptsResponse>
  return { system: data.system ?? null, user: data.user ?? null }
}

export function fetchEnvelopes(adwId: string): Promise<Envelope[]> {
  return getJson(`/api/sessions/${encodeURIComponent(adwId)}/envelopes`) as Promise<Envelope[]>
}

export function fetchGates(adwId: string): Promise<GateResult[]> {
  return getJson(`/api/sessions/${encodeURIComponent(adwId)}/gates`) as Promise<GateResult[]>
}
