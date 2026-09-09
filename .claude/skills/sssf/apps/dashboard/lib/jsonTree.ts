/**
 * Shape classification for the artifact payload viewer.
 *
 * Kept out of the component so the rules a reader depends on — what counts as
 * expandable, what an entry is labelled, how a leaf is written — are testable
 * without rendering anything.
 */

export type JsonEntry = { key: string; value: unknown };

/** A value the viewer can descend into. Everything else is a leaf. */
export function isBranch(value: unknown): value is Record<string, unknown> | unknown[] {
  return value !== null && typeof value === "object";
}

/** Named children of a branch, in the order the payload stores them. */
export function entriesOf(value: Record<string, unknown> | unknown[]): JsonEntry[] {
  return Array.isArray(value)
    ? value.map((item, index) => ({ key: String(index), value: item }))
    : Object.entries(value).map(([key, item]) => ({ key, value: item }));
}

/**
 * What a collapsed branch says about itself, so a reader can decide whether to
 * open it without opening it. An empty branch says so rather than promising
 * content it does not have.
 */
export function branchSummary(value: Record<string, unknown> | unknown[]): string {
  const count = entriesOf(value).length;
  if (Array.isArray(value)) return count === 1 ? "1 item" : `${count} items`;
  return count === 1 ? "1 key" : `${count} keys`;
}

/** A leaf rendered as source, so `null`, `false` and `""` stay distinguishable. */
export function leafText(value: unknown): string {
  if (typeof value === "string") return value;
  return JSON.stringify(value) ?? "undefined";
}

/** CSS modifier naming a leaf's type, so the palette can separate them. */
export function leafTone(value: unknown): string {
  if (value === null) return "null";
  switch (typeof value) {
    case "string":
      return "string";
    case "number":
      return "number";
    case "boolean":
      return "boolean";
    default:
      return "other";
  }
}

/**
 * Branches open on first render.
 *
 * A payload's own keys are worth seeing without a click. One level in is where
 * a `public_contract` or a `tree_delta` would flood the row, so it stays shut
 * until asked for — except the first finding, because a review artifact whose
 * findings are the reason it exists should not need two clicks to read one.
 */
export function opensByDefault(depth: number, key: string, parentKey?: string): boolean {
  if (depth === 0) return true;
  if (depth !== 1) return false;
  return key === "0" && (parentKey === "findings" || parentKey === "advisory_findings");
}
