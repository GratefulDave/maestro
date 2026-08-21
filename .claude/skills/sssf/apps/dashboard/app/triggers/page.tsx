import { EmptyState } from "@/components/EmptyState";
import { PageHeader } from "@/components/PageHeader";
import { SourceBanner } from "@/components/SourceBanner";

export default function TriggersPage() {
  return (
    <section className="page-stack">
      <PageHeader
        eyebrow="Operate"
        title="Triggers"
        description="Triggers would launch runs. This console does not."
      />
      <SourceBanner label="Dispatch is not wired" tone="info" />
      <EmptyState
        title="Dispatch is not wired"
        description="Triggers are a start path. They are not implemented in this commit."
      />
    </section>
  );
}
