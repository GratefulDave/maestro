import type { ReactNode } from "react";
import { Circle } from "@/lib/icons";

export function EmptyState({
  title,
  description,
  action,
}: {
  title: string;
  description?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="empty-state panel-2">
      <Circle size={22} />
      <div>
        <strong>{title}</strong>
        {description && <div className="empty-description">{description}</div>}
      </div>
      {action}
    </div>
  );
}
