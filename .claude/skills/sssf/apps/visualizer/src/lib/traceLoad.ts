import type {
  AgentSession,
  Envelope,
  EventRow,
  GateResult,
  Phase,
  Session,
  SessionUsage,
} from './types'

export type TracePayload = {
  session: Session | null
  phases: Phase[]
  agents: AgentSession[]
  usage: SessionUsage
  events: EventRow[]
  envelopes: Envelope[]
  gates: GateResult[]
  cursor: number
  loaded: boolean
  apiError: string | null
}

export function emptyTracePayload(): TracePayload {
  return {
    session: null,
    phases: [],
    agents: [],
    usage: { read: 0, written: 0 },
    events: [],
    envelopes: [],
    gates: [],
    cursor: 0,
    loaded: false,
    apiError: null,
  }
}


/**
 * What the trace view shows after a failed load.
 *
 * A failed load must not leave the previous session's panes on screen —
 * the operator otherwise reads stale data as current.
 */
export function tracePayloadAfterError(_prev: TracePayload, err: unknown): TracePayload {
  return {
    ...emptyTracePayload(),
    apiError: err instanceof Error ? err.message : String(err),
  }
}
