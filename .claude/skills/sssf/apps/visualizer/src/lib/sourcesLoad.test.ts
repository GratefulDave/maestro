import { describe, expect, test } from 'bun:test'
import { ApiHttpError } from './api'
import { sourcesErrorMessage } from './sourcesLoad'

describe('sourcesErrorMessage', () => {
  test('names the missing API and the start command that actually works', () => {
    const message = sourcesErrorMessage(new TypeError('Failed to fetch'))
    expect(message).toContain('API on :4600')
    expect(message).toContain('dev:all')
    expect(message).toContain('just -g viz')
  })

  test('treats a Vite-proxy 500 as the API being down', () => {
    const message = sourcesErrorMessage(new ApiHttpError(500, '/api/sources'))
    expect(message).toContain('API on :4600')
    expect(message).toContain('dev:all')
  })

  test('still names a non-absent failure', () => {
    expect(sourcesErrorMessage(new ApiHttpError(401, '/api/sources'))).toContain(
      'failed to load sources',
    )
  })
})
