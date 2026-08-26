"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import type { RunHierarchy } from "@/lib/runHierarchy";
import { decodeRouteParam } from "@/lib/href";

export const HIERARCHY_LIVE_REFRESH_MS = 3_000;
export const HIERARCHY_TERMINAL_REFRESH_MS = 30_000;

/** Retry failures promptly, poll live runs promptly, and back off terminal runs. */
export function hierarchyRefreshDelay(hierarchy: RunHierarchy | null): number {
  return hierarchy?.is_live
    ? HIERARCHY_LIVE_REFRESH_MS
    : hierarchy
      ? HIERARCHY_TERMINAL_REFRESH_MS
      : HIERARCHY_LIVE_REFRESH_MS;
}

function runRoute(pathname: string): { sourceId: string; runId: string } | null {
  const match = /^\/runs\/([^/]+)\/([^/]+)/.exec(pathname);
  if (!match) return null;
  return { sourceId: match[1], runId: match[2] };
}

export function RunHierarchyNav({ pathname }: { pathname: string }) {
  const route = runRoute(pathname);
  const sourceId = route?.sourceId;
  const runId = route?.runId;
  const hierarchyKey = sourceId && runId ? `${sourceId}/${runId}` : null;
  const [snapshot, setSnapshot] = useState<{
    key: string;
    hierarchy: RunHierarchy | null;
  } | null>(null);
  const hierarchy = snapshot?.key === hierarchyKey ? snapshot.hierarchy : null;

  useEffect(() => {
    if (!sourceId || !runId || !hierarchyKey) return;
    const controller = new AbortController();
    const encodedSourceId = encodeURIComponent(decodeRouteParam(sourceId));
    const encodedRunId = encodeURIComponent(decodeRouteParam(runId));
    let stopped = false;
    let timeout: NodeJS.Timeout | undefined;

    const load = async () => {
      let next: RunHierarchy | null = null;
      try {
        const response = await fetch(`/api/run-nav/${encodedSourceId}/${encodedRunId}`, {
          cache: "no-store",
          signal: controller.signal,
        });
        if (!response.ok) throw new Error(`hierarchy request failed: ${response.status}`);
        next = (await response.json()) as RunHierarchy;
        if (!stopped) setSnapshot({ key: hierarchyKey, hierarchy: next });
      } catch (error: unknown) {
        if (error instanceof DOMException && error.name === "AbortError") return;
        // Keep the last good hierarchy visible. A failed first request remains
        // empty, but uses the same timer below to retry instead of disappearing.
      }
      if (!stopped) {
        timeout = setTimeout(() => void load(), hierarchyRefreshDelay(next));
      }
    };

    void load();
    return () => {
      stopped = true;
      controller.abort();
      clearTimeout(timeout);
    };
  }, [hierarchyKey, sourceId, runId]);

  if (!hierarchy) return null;

  return (
    <section className="run-hierarchy" aria-label="Current workspace hierarchy">
      <p className="nav-label">Workspace</p>
      <Link className="workspace-nav-link" href={hierarchy.href} title={hierarchy.label}>
        {hierarchy.label}
      </Link>
      <ul className="lane-nav-list">
        {hierarchy.lanes.map((lane) => (
          <li key={lane.id}>
            <details open>
              <summary>
                <span className="lane-nav-name">{lane.label}</span>
                <span className={`hierarchy-state hierarchy-state-${lane.state.toLowerCase()}`}>
                  {lane.state}
                </span>
              </summary>
              <ul className="agent-nav-list">
                {lane.agents.map((agent) => {
                  const active = pathname === agent.href;
                  return (
                    <li key={agent.id}>
                      <Link
                        aria-current={active ? "page" : undefined}
                        className={`agent-nav-link${active ? " agent-nav-link-active" : ""}`}
                        href={agent.href}
                        title={`${agent.id} · ${agent.state}`}
                      >
                        <span className={`agent-role agent-role-${agent.role}`} aria-hidden="true" />
                        <span>{agent.label}</span>
                        <small>{agent.state}</small>
                      </Link>
                    </li>
                  );
                })}
              </ul>
            </details>
          </li>
        ))}
      </ul>
    </section>
  );
}
