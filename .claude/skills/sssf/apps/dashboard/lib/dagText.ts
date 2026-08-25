import type { MaestroNode } from "@/lib/types";

export interface DagTextRow {
  node: MaestroNode;
  rail: string;
  offTreeNeeds: string[];
}

/**
 * Project a DAG onto one readable primary tree without hiding any edge.
 *
 * The first declared dependency owns the structural rail, matching the
 * git-log-style grammar used by herdr-dagr. Every additional dependency is
 * retained as an explicit off-tree reference. Cycles and malformed primary
 * links fall back to roots and keep the undisplayed link as an off-tree edge.
 */
export function buildDagTextRows(nodes: MaestroNode[]): DagTextRow[] {
  const indexById = new Map<string, number>();
  nodes.forEach((node, index) => {
    if (!indexById.has(node.node_id)) indexById.set(node.node_id, index);
  });

  const primaryParent = nodes.map((node, index) => {
    const primary = node.needs[0];
    const parent = primary === undefined ? undefined : indexById.get(primary);
    return parent === index ? undefined : parent;
  });
  const children = nodes.map(() => [] as number[]);
  primaryParent.forEach((parent, index) => {
    if (parent !== undefined) children[parent].push(index);
  });

  const rows: DagTextRow[] = [];
  const visited = new Set<number>();

  function walk(index: number, continuations: boolean[], rail: string, primaryDrawn: boolean) {
    if (visited.has(index)) return;
    visited.add(index);

    const node = nodes[index];
    const hiddenPrimary = primaryDrawn ? node.needs.slice(1) : node.needs;
    rows.push({ node, rail, offTreeNeeds: hiddenPrimary });

    const unvisitedChildren = children[index].filter((child) => !visited.has(child));
    unvisitedChildren.forEach((child, position) => {
      const hasNext = position < unvisitedChildren.length - 1;
      const childRail =
        continuations.map((continues) => (continues ? "│  " : "   ")).join("") +
        (hasNext ? "├─ " : "╰─ ");
      walk(child, [...continuations, hasNext], childRail, true);
    });
  }

  primaryParent.forEach((parent, index) => {
    if (parent === undefined) walk(index, [], "", false);
  });

  // A valid DAG is exhausted above. Remaining rows belong to malformed cycles;
  // render each component once and expose its undisplayed primary edge.
  nodes.forEach((_, index) => {
    if (!visited.has(index)) walk(index, [], "", false);
  });

  return rows;
}
