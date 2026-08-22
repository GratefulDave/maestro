import type { ReactNode } from "react";
import { Activity } from "@/lib/icons";

export function SourceBanner({
  label,
  detail,
  tone = "info",
}: {
  label: string;
  detail?: ReactNode;
  tone?: "info" | "warning" | "error";
}) {
  return (
    <div className={`source-banner source-${tone}`}>
      <Activity size={16} />
      <strong>{label}</strong>
      {detail ? <span>{detail}</span> : null}
    </div>
  );
}
