import {
  CheckCircle2,
  Circle,
  Clock,
  Loader2,
  Shield,
  XCircle,
} from "@/lib/icons";
import type { LucideIcon } from "@/lib/icons";

const STATUS_ICONS: Record<string, LucideIcon> = {
  merged: CheckCircle2,
  verified: CheckCircle2,
  accepted: CheckCircle2,
  ok: CheckCircle2,
  running: Loader2,
  cancelling: Loader2,
  reviewing: Shield,
  pending: Circle,
  quiescent: Circle,
  empty: Circle,
  ready: Clock,
  blocked: XCircle,
  cancelled: XCircle,
  stuck: XCircle,
  failed: XCircle,
  stale: Clock,
  unknown: Clock,
  not_recorded: Circle,
  not_running: Circle,
};

const STATUS_TONES: Record<string, string> = {
  merged: "ok",
  verified: "ok",
  accepted: "ok",
  ok: "ok",
  running: "info",
  cancelling: "warn",
  reviewing: "review",
  pending: "pending",
  quiescent: "pending",
  empty: "pending",
  ready: "ready",
  blocked: "warn",
  warning: "warn",
  cancelled: "err",
  stuck: "err",
  failed: "err",
  stale: "warn",
  unknown: "pending",
  not_recorded: "pending",
  not_running: "pending",
};

export function StatusPill({
  status,
  label,
  title,
}: {
  status: string;
  label?: string;
  title?: string;
}) {
  const normalized = status.toLowerCase();
  const Icon = STATUS_ICONS[normalized] ?? Circle;
  const tone = STATUS_TONES[normalized] ?? "pending";
  const spinning = normalized === "running" || normalized === "cancelling";

  return (
    <span className={`status-pill status-${tone}`} title={title}>
      <Icon className={spinning ? "status-spin" : undefined} size={14} />
      <span>{label ?? status.replaceAll("_", " ")}</span>
    </span>
  );
}
