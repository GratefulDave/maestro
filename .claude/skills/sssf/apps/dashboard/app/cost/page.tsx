import { EmptyState } from "@/components/EmptyState";
import { PageHeader } from "@/components/PageHeader";
import { SourceBanner } from "@/components/SourceBanner";

export const dynamic = "force-dynamic";

export default function CostPage() {
  return (
    <section className="page-stack">
      <PageHeader
        eyebrow="Fleet analytics"
        title="Cost"
        description="The lifecycle ledger publishes no usage or pricing fields."
      />
      <SourceBanner
        label="No token or cost columns"
        detail="MaestroAttempt.session_path and turn_count exist; parsing those logs is not in this commit."
        tone="warning"
      />
      <EmptyState
        title="The lifecycle ledger publishes no usage or pricing fields"
        description="Cost will later be back-filled from agent session logs. This route stays so filling it is a data change, not a UI rebuild."
      />
    </section>
  );
}
