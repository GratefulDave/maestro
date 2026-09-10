"use client";

import { useState } from "react";

import {
  branchSummary,
  entriesOf,
  isBranch,
  leafText,
  leafTone,
  opensByDefault,
} from "@/lib/jsonTree";

/**
 * The artifact body, drawn as a tree the reader opens a level at a time.
 *
 * Hand-written rather than a JSON-viewer dependency: the whole renderer is
 * two components, and the one thing a library would not know is which keys are
 * worth opening on sight (`lib/jsonTree.opensByDefault`).
 *
 * Everything here is already public. The Bun API publishes an artifact body
 * through a per-kind key allowlist (`visualizer/server/artifactFactoryDb.ts`),
 * never `payload_json` wholesale, so a draft's source, selectors and expected
 * literals never reach this component to be rendered.
 */
function JsonNode({
  entryKey,
  value,
  depth,
  parentKey,
}: {
  entryKey: string;
  value: unknown;
  depth: number;
  parentKey?: string;
}) {
  if (!isBranch(value)) {
    return (
      <div className="json-leaf" style={{ paddingLeft: `${depth * 14}px` }}>
        <span className="json-key">{entryKey}</span>
        <span className={`json-value json-${leafTone(value)}`}>{leafText(value)}</span>
      </div>
    );
  }

  const children = entriesOf(value);
  return (
    <details
      className="json-branch"
      open={opensByDefault(depth, entryKey, parentKey)}
      style={{ paddingLeft: `${depth * 14}px` }}
    >
      <summary>
        <span className="json-key">{entryKey}</span>
        <span className="json-brace">{Array.isArray(value) ? "[ … ]" : "{ … }"}</span>
        <span className="json-count">{branchSummary(value)}</span>
      </summary>
      {children.map((child) => (
        <JsonNode
          depth={depth + 1}
          entryKey={child.key}
          key={child.key}
          parentKey={entryKey}
          value={child.value}
        />
      ))}
    </details>
  );
}

function CopyButton({ value }: { value: unknown }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      className="json-copy"
      onClick={() => {
        void navigator.clipboard
          .writeText(JSON.stringify(value, null, 2))
          .then(() => {
            setCopied(true);
            window.setTimeout(() => setCopied(false), 1500);
          })
          .catch(() => setCopied(false));
      }}
      type="button"
    >
      {copied ? "copied" : "copy JSON"}
    </button>
  );
}

export function ArtifactPayload({ payload }: { payload: unknown }) {
  if (!isBranch(payload)) {
    return <p className="muted">This artifact records no body.</p>;
  }
  const entries = entriesOf(payload);
  if (entries.length === 0) {
    return <p className="muted">This artifact records no body.</p>;
  }
  return (
    <div className="json-tree">
      <div className="json-toolbar">
        <CopyButton value={payload} />
      </div>
      {entries.map((entry) => (
        <JsonNode depth={0} entryKey={entry.key} key={entry.key} value={entry.value} />
      ))}
    </div>
  );
}
