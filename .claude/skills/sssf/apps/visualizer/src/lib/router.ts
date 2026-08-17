import { ref } from 'vue'

/**
 * Hash routes.
 *
 * Tracer (unchanged, so existing links keep working):
 *   #/                       → sessions
 *   #/<adw_id>               → waterfall
 *   #/<adw_id>/<phase_id>    → phase panel open
 *
 * A non-tracer source is addressed under an `s/` prefix, which is what keeps
 * the two namespaces apart: a source id could otherwise be mistaken for an
 * adw_id, and the tracer route has to stay the bare one.
 *   #/s/<source_id>          → that source's run index
 *   #/s/<source_id>/<run_id> → one run
 */
export interface Route {
  /** Non-tracer source, when the route names one. */
  sourceId: string | null
  /** Maestro run id, only ever set alongside `sourceId`. */
  runId: string | null
  adwId: string | null
  phaseId: string | null
}

function parse(): Route {
  const parts = window.location.hash
    .replace(/^#\/?/, '')
    .split('/')
    .filter(Boolean)
    .map(decodeURIComponent)
  if (parts[0] === 's') {
    return {
      sourceId: parts[1] ?? null,
      runId: parts[2] ?? null,
      adwId: null,
      phaseId: null,
    }
  }
  return {
    sourceId: null,
    runId: null,
    adwId: parts[0] ?? null,
    phaseId: parts[1] ?? null,
  }
}

const route = ref<Route>(parse())

window.addEventListener('hashchange', () => {
  route.value = parse()
})

export function useRoute() {
  return route
}

// Display name for the phase crumb — set by the trace view once phases load,
// since the phase_id in the URL is not the display name.
export const phaseCrumb = ref<string | null>(null)

export function hrefFor(adwId?: string | null, phaseId?: string | null): string {
  let h = '#/'
  if (adwId) h += encodeURIComponent(adwId)
  if (adwId && phaseId) h += `/${encodeURIComponent(phaseId)}`
  return h
}

export function navigate(adwId?: string | null, phaseId?: string | null): void {
  window.location.hash = hrefFor(adwId, phaseId)
}

export function hrefForSource(sourceId: string, runId?: string | null): string {
  let h = `#/s/${encodeURIComponent(sourceId)}`
  if (runId) h += `/${encodeURIComponent(runId)}`
  return h
}
