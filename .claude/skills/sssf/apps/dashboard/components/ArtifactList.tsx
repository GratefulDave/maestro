import { ArtifactPayload } from "@/components/ArtifactPayload";
import { EmptyState } from "@/components/EmptyState";
import { StatusPill } from "@/components/StatusPill";
import { Timestamp } from "@/components/Timestamp";
import type { MaestroResult } from "@/lib/types";

/**
 * Every result / adjudication artifact the run recorded, newest last, each one
 * openable to its body.
 *
 * The collapsed row carries what identifies the artifact — sequence, kind,
 * lane, when, and the ref its bytes are pinned under — so a run with sixteen
 * test-review rounds reads as a list rather than sixteen identical lines. The
 * body is drawn by `ArtifactPayload`, and is whatever the Bun API's per-kind
 * allowlist published; nothing here reaches past that.
 *
 * Expansion is a native `<details>`, so the list works before hydration and
 * the only client code on the page is the copy button.
 */
export function ArtifactList({ results }: { results: MaestroResult[] }) {
  if (results.length === 0) {
    return (
      <EmptyState
        title="No artifacts"
        description="This run has recorded no result or adjudication artifact."
      />
    );
  }

  return (
    <ul className="artifact-list">
      {results.map((result, index) => {
        const kind = result.artifact_kind;
        return (
          <li key={`${index}-${result.sequence ?? ""}-${result.node_id ?? "run"}`}>
            <details className="artifact-entry">
              <summary>
                <span className="artifact-seq">
                  {result.sequence === null ? "—" : `#${result.sequence}`}
                </span>
                <span className="artifact-kind">{kind ?? "RESULT"}</span>
                <span className="artifact-lane">
                  {result.node_id ?? <span className="muted">run-level</span>}
                </span>
                {result.adjudication ? (
                  <StatusPill status={result.adjudication} />
                ) : (
                  <span className="muted">—</span>
                )}
                <span className="artifact-when">
                  <Timestamp value={result.created_at} />
                </span>
                <code className="artifact-ref" title={result.artifact_ref ?? undefined}>
                  {result.artifact_ref ?? "—"}
                </code>
              </summary>
              <div className="artifact-body">
                <ArtifactPayload payload={result.payload} />
              </div>
            </details>
          </li>
        );
      })}
    </ul>
  );
}
