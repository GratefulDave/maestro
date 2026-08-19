import process from 'node:process'

/**
 * `bun run dev` used to start Vite alone on :4601. The API on :4600 never
 * came up, startup still looked successful, and the UI sat on
 * "loading sources…" forever. Refuse instead of producing that half-app.
 */
const message = `bun run dev starts only the Vite frontend on :4601 and leaves the API on :4600 down.

Start both halves:
  just -g viz
  bun run --cwd <visualizer-dir> dev:all

Frontend only, API already running:
  bun run --cwd <visualizer-dir> dev:ui
`
console.error(message)
process.exit(1)
