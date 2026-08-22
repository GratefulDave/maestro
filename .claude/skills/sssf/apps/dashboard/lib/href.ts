export function decodeRouteParam(value: string): string {
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}

/** Decode first, encode once. Safe from an API id or a Next route param. */
export function runHref(sourceId: string, runId: string, suffix = ""): string {
  return `/runs/${encodeURIComponent(decodeRouteParam(sourceId))}/${encodeURIComponent(decodeRouteParam(runId))}${suffix}`;
}
