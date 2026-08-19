import { ApiHttpError } from './api'

function isApiAbsent(err: unknown): boolean {
  if (err instanceof TypeError) return true
  if (typeof DOMException !== 'undefined' && err instanceof DOMException && err.name === 'TimeoutError') {
    return true
  }
  const text = err instanceof Error ? err.message : String(err)
  if (/ECONNREFUSED|Failed to fetch|NetworkError|fetch failed|aborted|timeout/i.test(text)) {
    return true
  }
  return err instanceof ApiHttpError && err.status >= 500 && err.status <= 504
}

/** Operator-facing copy when `/api/sources` cannot be loaded. */
export function sourcesErrorMessage(err: unknown): string {
  if (isApiAbsent(err)) {
    return 'API on :4600 is not running. Start both halves with `just -g viz` or `bun run dev:all`.'
  }
  return `failed to load sources — ${err instanceof Error ? err.message : String(err)}`
}
