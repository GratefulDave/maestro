import type { ReactNode } from "react";
import { Breadcrumbs } from "@/components/Breadcrumbs";
import { EmptyState } from "@/components/EmptyState";
import { PageHeader } from "@/components/PageHeader";
import { RunTabs } from "@/components/RunTabs";
import { SourceBanner } from "@/components/SourceBanner";
import { StatusPill } from "@/components/StatusPill";
import { ApiUnavailable } from "@/components/ApiUnavailable";
import { getRun } from "@/lib/api";
import type { MaestroRunDetail } from "@/lib/types";

export async function loadRunOrBanner(
  sourceId: string,
  runId: string,
): Promise<
  | { ok: false; page: ReactNode }
  | { ok: true; run: MaestroRunDetail }
> {
  const result = await getRun(sourceId, runId);
  if (!result.ok && result.kind === "unreachable") {
    return {
      ok: false,
      page: (
        <section className="page-stack">
          <Breadcrumbs
            items={[
              { label: "Runs", href: "/runs" },
              { label: sourceId },
              { label: runId },
            ]}
          />
          <PageHeader eyebrow="Run" title={runId} />
          <ApiUnavailable message={result.message} />
        </section>
      ),
    };
  }
  if (!result.ok) {
    return {
      ok: false,
      page: (
        <section className="page-stack">
          <Breadcrumbs
            items={[
              { label: "Runs", href: "/runs" },
              { label: sourceId },
              { label: runId },
            ]}
          />
          <PageHeader eyebrow="Run" title={runId} />
          <SourceBanner label="Run missing" detail={result.message} tone="warning" />
          <EmptyState
            title="This run is not in the ledger"
            description="The Bun API answered, but GET /api/sources/:source_id/runs/:run_id returned no row."
          />
        </section>
      ),
    };
  }
  return { ok: true, run: result.data };
}

export function RunChrome({
  sourceId,
  runId,
  run,
  children,
}: {
  sourceId: string;
  runId: string;
  run: MaestroRunDetail;
  children: ReactNode;
}) {
  return (
    <section className="page-stack">
      <Breadcrumbs
        items={[
          { label: "Runs", href: "/runs" },
          { label: sourceId, href: "/runs" },
          { label: runId },
        ]}
      />
      <PageHeader
        eyebrow="Run"
        title={run.plan_name ?? run.plan_digest}
        description={
          <>
            <code>{run.run_id}</code>
            {run.plan_name == null && (
              <>
                {" "}
                · plan_name is null — digest matches no installed plan directory
              </>
            )}
          </>
        }
        meta={
          <>
            <StatusPill status={run.state} />
            {run.declared_outcome && (
              <>
                {" "}
                declared <StatusPill status={run.declared_outcome} />
              </>
            )}
          </>
        }
      />
      <RunTabs runId={runId} sourceId={sourceId} />
      {children}
    </section>
  );
}
