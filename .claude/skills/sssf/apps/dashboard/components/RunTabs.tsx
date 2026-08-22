"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { runHref } from "@/lib/href";

const TABS = [
  { suffix: "", label: "Report" },
  { suffix: "/plan", label: "Plan" },
  { suffix: "/lanes", label: "Lanes" },
  { suffix: "/gates", label: "Gates" },
  { suffix: "/inspect", label: "Inspect" },
] as const;

export function RunTabs({ sourceId, runId }: { sourceId: string; runId: string }) {
  const pathname = usePathname();
  const base = runHref(sourceId, runId);
  return (
    <nav className="run-tabs" aria-label="Run detail">
      {TABS.map((tab) => {
        const href = `${base}${tab.suffix}`;
        const active = pathname === href;
        return (
          <Link className={active ? "run-tab-active" : undefined} href={href} key={tab.label}>
            {tab.label}
          </Link>
        );
      })}
    </nav>
  );
}
