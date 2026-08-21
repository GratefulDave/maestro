import { EmptyState } from "@/components/EmptyState";
import { PageHeader } from "@/components/PageHeader";
import { SourceBanner } from "@/components/SourceBanner";

export default function WorkspacePage() {
  return (
    <section className="page-stack">
      <PageHeader
        eyebrow="Operate"
        title="Workspaces"
        description="Workspace inventory is not a lifecycle-ledger surface."
      />
      <SourceBanner
        label="No workspace inventory"
        detail="Integration path/branch/head live on each run detail, not as a fleet workspace list."
        tone="warning"
      />
      <EmptyState
        title="No workspace inventory in the ledger"
        description="Open a run report to see that run's integration path. There is no fleet-wide workspace table."
      />
    </section>
  );
}
