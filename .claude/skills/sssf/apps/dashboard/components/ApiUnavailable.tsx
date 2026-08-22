import { EmptyState } from "@/components/EmptyState";
import { SourceBanner } from "@/components/SourceBanner";

export function ApiUnavailable({ message }: { message: string }) {
  return (
    <>
      <SourceBanner
        label="Bun API unreachable"
        detail={message}
        tone="error"
      />
      <EmptyState
        title="Cannot reach the lifecycle API"
        description="This is not an empty ledger. The dashboard fetches http://localhost:${MAESTRO_API_PORT ?? 4600}/api/... and that request failed. Start the visualizer API, then reload."
      />
    </>
  );
}
