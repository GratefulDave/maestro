import Link from "next/link";
import { useMemo } from "react";
import { STATUS_TONES } from "@/lib/status";
import { buildDagTextRows } from "@/lib/dagText";
import type { MaestroNode } from "@/lib/types";

const TONE_ORDER = ["ok", "info", "warn", "err", "review", "ready", "pending"];

const TONE_MARKERS: Record<string, string> = {
  ok: "●",
  info: "◎",
  warn: "▲",
  err: "✗",
  review: "◆",
  ready: "◇",
  pending: "○",
};

function stateLabel(state: string, tone: string) {
  const text = state.replaceAll("_", " ").toLowerCase();
  if (tone === "ok") return `${text} ◆`;
  if (tone === "err") return `${text} ✗`;
  return text;
}


export function DagCockpit({ nodes, base }: { nodes: MaestroNode[]; base: string }) {
  const rollup = useMemo(() => {
    const counts = new Map<string, number>();
    for (const node of nodes) {
      counts.set(node.state, (counts.get(node.state) ?? 0) + 1);
    }
    const byTone = new Map<string, [string, number][]>();
    for (const [state, count] of counts) {
      const tone = STATUS_TONES[state.toLowerCase()] ?? "pending";
      const group = byTone.get(tone) ?? [];
      group.push([state, count]);
      byTone.set(tone, group);
    }
    return TONE_ORDER.flatMap((tone) =>
      (byTone.get(tone) ?? []).map(([state, count]) => ({
        tone,
        label: `${count} ${state.replaceAll("_", " ")}`,
      })),
    );
  }, [nodes]);

  const ordered = useMemo(() => buildDagTextRows(nodes), [nodes]);

  return (
    <div className="cockpit">
      <p className="cockpit-header">
        {rollup.length === 0 ? (
          <span className="muted">no nodes</span>
        ) : (
          rollup.map((entry, index) => (
            <span key={entry.label}>
              {index > 0 ? <span className="muted"> · </span> : null}
              <span className={`status-${entry.tone}`}>{entry.label}</span>
            </span>
          ))
        )}
      </p>
      <div className="cockpit-ledger">
        {ordered.map(({ node, rail, offTreeNeeds }) => {
          const tone = STATUS_TONES[node.state.toLowerCase()] ?? "pending";
          const note = node.block_reason ?? node.cancel_cause;
          return (
            <Link
              className={`cockpit-row cockpit-row-${tone}`}
              href={`${base}/${encodeURIComponent(node.node_id)}`}
              key={`${node.node_id}:${node.attempt_no}`}
            >
              <span className="cockpit-branch">{rail}</span>
              <span aria-hidden className="cockpit-marker">
                {TONE_MARKERS[tone] ?? "○"}
              </span>
              <strong className="cockpit-id">{node.node_id}</strong>
              <span className="cockpit-kind">{node.kind ?? "node"}</span>
              {offTreeNeeds.map((need) => (
                <span className="cockpit-dependency" key={need}>
                  ⇠ {need}
                </span>
              ))}
              <span className="cockpit-state">{stateLabel(node.state, tone)}</span>
              <span className="cockpit-meta">
                {node.attempt_no > 1 ? `a${node.attempt_no} ` : ""}
                {note ? `· ${note}` : ""}
              </span>
            </Link>
          );
        })}
      </div>
    </div>
  );
}
