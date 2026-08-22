import { EmptyState } from "@/components/EmptyState";
import { PageHeader } from "@/components/PageHeader";
import { SourceBanner } from "@/components/SourceBanner";

export default function DispatchPage() {
  return (
    <section className="page-stack">
      <PageHeader
        eyebrow="Operate"
        title="Dispatch"
        description="Dispatch is not wired. This console never starts a run."
      />
      <SourceBanner
        label="Dispatch is not wired"
        detail="A UI button that starts a run is starting a run unasked."
        tone="info"
      />
      <EmptyState
        title="Dispatch is not wired"
        description="This dashboard does not create plans, start runs, or invoke the CLI. Use the Maestro CLI directly."
      />
    </section>
  );
}
