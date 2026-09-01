import {
  CheckCircle2,
  Circle,
  Clock,
  Loader2,
  Shield,
  XCircle,
} from "@/lib/icons";
import type { LucideIcon } from "@/lib/icons";
import { STATUS_TONES } from "@/lib/status";

const STATUS_ICONS: Record<string, LucideIcon> = {
  planned: Circle,
  writing_tests: Loader2,
  reviewing_tests: Shield,
  tests_sealed: CheckCircle2,
  building: Loader2,
  reviewing_code: Shield,
  ready_to_merge: Clock,
  waiting_for_user: Clock,
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
  abandoned: Clock,
  unknown: Clock,
  not_recorded: Circle,
  not_running: Circle,

};

const SPINNING_STATUSES = new Set([
  "running",
  "cancelling",
  "writing_tests",
  "building",
]);

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
  const spinning = SPINNING_STATUSES.has(normalized);

  return (
    <span className={`status-pill status-${tone}`} title={title}>
      <Icon className={spinning ? "status-spin" : undefined} size={14} />
      <span>{label ?? status.replaceAll("_", " ")}</span>
    </span>
  );
}
