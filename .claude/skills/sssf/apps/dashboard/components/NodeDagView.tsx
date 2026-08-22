"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import { Background, Controls, MarkerType, ReactFlow, type Edge, type Node } from "@xyflow/react";
import { Activity, GitFork } from "@/lib/icons";
import { runHref } from "@/lib/href";
import type { MaestroNode } from "@/lib/types";
import { StatusPill } from "./StatusPill";

export function NodeDagView({
  sourceId,
  runId,
  nodes,
}: {
  sourceId: string;
  runId: string;
  nodes: MaestroNode[];
}) {
  const [mode, setMode] = useState<"table" | "dag">("table");
  const base = `${runHref(sourceId, runId)}/lanes`;
  const flow = useMemo(() => {
    const byDepth = new Map<number, MaestroNode[]>();
    for (const node of nodes) {
      const group = byDepth.get(node.depth) ?? [];
      group.push(node);
      byDepth.set(node.depth, group);
    }
    const flowNodes: Node[] = [];
    for (const [depth, group] of [...byDepth.entries()].sort(([a], [b]) => a - b)) {
      group.forEach((node, index) => {
        flowNodes.push({
          id: node.node_id,
          position: { x: index * 220, y: depth * 120 },
          data: {
            label: (
              <Link className="dag-node-content" href={`${base}/${encodeURIComponent(node.node_id)}`}>
                <strong>{node.node_id}</strong>
                <small>
                  {node.kind ?? "node"} · {node.state}
                </small>
              </Link>
            ),
          },
          className: `dag-node dag-node-${node.state.toLowerCase()}`,
        });
      });
    }
    const edges: Edge[] = nodes.flatMap((node) =>
      node.needs.map((need) => ({
        id: `${need}->${node.node_id}`,
        source: need,
        target: node.node_id,
        markerEnd: { type: MarkerType.ArrowClosed },
      })),
    );
    return { nodes: flowNodes, edges };
  }, [base, nodes]);

  return (
    <section className="panel section-panel">
      <div className="section-heading">
        <div>
          <h2>Nodes</h2>
          <p>DAG edges come from each node&apos;s needs list.</p>
        </div>
        <div className="view-toggle">
          <button
            className={mode === "table" ? "view-toggle-active" : undefined}
            onClick={() => setMode("table")}
            type="button"
          >
            <Activity size={14} /> Table
          </button>
          <button
            className={mode === "dag" ? "view-toggle-active" : undefined}
            onClick={() => setMode("dag")}
            type="button"
          >
            <GitFork size={14} /> DAG
          </button>
        </div>
      </div>
      {mode === "dag" ? (
        <div className="dag-canvas">
          <ReactFlow
            edges={flow.edges}
            fitView
            nodes={flow.nodes}
            nodesDraggable={false}
            nodesConnectable={false}
            proOptions={{ hideAttribution: true }}
          >
            <Background />
            <Controls />
          </ReactFlow>
        </div>
      ) : (
        <div className="table-wrap">
          <table className="data-table">
            <thead>
              <tr>
                <th>Node</th>
                <th>Kind</th>
                <th>State</th>
                <th>Needs</th>
                <th>Attempt</th>
              </tr>
            </thead>
            <tbody>
              {nodes.map((node) => (
                <tr key={node.node_id}>
                  <td>
                    <Link className="lane-link" href={`${base}/${encodeURIComponent(node.node_id)}`}>
                      {node.node_id}
                    </Link>
                  </td>
                  <td>{node.kind ?? "—"}</td>
                  <td>
                    <StatusPill status={node.state} />
                  </td>
                  <td>
                    {node.needs.length === 0 ? (
                      <span className="muted">none</span>
                    ) : (
                      <span className="dependency-list">
                        {node.needs.map((need) => (
                          <span className="dependency-chip" key={need}>
                            {need}
                          </span>
                        ))}
                      </span>
                    )}
                  </td>
                  <td>{node.attempt_no}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
