import { expect, test } from 'bun:test'
import { resolve } from 'node:path'

const viz = resolve(import.meta.dir, '..')

test('bun run dev refuses instead of starting Vite alone', async () => {
  const proc = Bun.spawn(['bun', 'run', 'dev'], {
    cwd: viz,
    stdout: 'pipe',
    stderr: 'pipe',
  })
  const [stdout, stderr, exit] = await Promise.all([
    new Response(proc.stdout).text(),
    new Response(proc.stderr).text(),
    proc.exited,
  ])
  const out = `${stdout}\n${stderr}`
  expect(exit).not.toBe(0)
  expect(out).toContain('dev:all')
  expect(out).toContain('just -g viz')
})
