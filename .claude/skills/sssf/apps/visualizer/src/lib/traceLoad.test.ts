import { describe, expect, test } from 'bun:test'
import type { Session } from './types'
import { emptyTracePayload, tracePayloadAfterError } from './traceLoad'

const previous: Session = {
  adw_id: 'session-a',
  adw_name: 'adw_plan',
  request: 'build the thing',
  status: 'success',
  started_at: '2026-08-19T00:00:00Z',
  ended_at: '2026-08-19T00:01:00Z',
  engineer: 'dave',
  total_cost: 1,
  total_tokens: 10,
} as Session

describe('tracePayloadAfterError', () => {
  test('clears the previous session so a failed load cannot be read as current', () => {
    const prev = {
      ...emptyTracePayload(),
      session: previous,
      phases: [{ phase_id: 'p1' } as never],
      events: [{ rowid: 1 } as never],
      envelopes: [{ id: 1 } as never],
      gates: [{ name: 'g' } as never],
      cursor: 99,
      loaded: true,
      apiError: null,
    }

    const next = tracePayloadAfterError(prev, new Error('GET /api/sessions/session-b → 404'))

    expect(next.session).toBeNull()
    expect(next.phases).toEqual([])
    expect(next.events).toEqual([])
    expect(next.envelopes).toEqual([])
    expect(next.gates).toEqual([])
    expect(next.cursor).toBe(0)
    expect(next.loaded).toBe(false)
    expect(next.apiError).toContain('GET /api/sessions/session-b → 404')
  })
})
