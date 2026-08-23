"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { NAV_ICONS } from "@/lib/icons";

const SECTIONS = [
  {
    label: "Observe",
    items: [
      { href: "/runs", label: "Runs", icon: "play" },
      { href: "/projects", label: "Projects", icon: "projects" },
      { href: "/agents", label: "Agents", icon: "agents" },
      { href: "/lanes", label: "Work items", icon: "git" },
      { href: "/plans", label: "Plans", icon: "clipboard" },
    ],
  },
  {
    label: "Operate",
    items: [
      { href: "/dispatch", label: "Dispatch", icon: "dispatch" },
      { href: "/gates", label: "Needs attention", icon: "shield" },
      { href: "/triggers", label: "Triggers", icon: "play" },
      { href: "/workspace", label: "Workspaces", icon: "clipboard" },
    ],
  },
  {
    label: "Analyze",
    items: [
      { href: "/analytics", label: "Analytics", icon: "chart" },
      { href: "/cost", label: "Cost", icon: "cost" },
      { href: "/federation", label: "Federation", icon: "globe" },
      { href: "/inspect", label: "Inspector", icon: "shield" },
    ],
  },
] as const;

export function Sidebar() {
  const pathname = usePathname();

  return (
    <aside className="sidebar">
      <Link className="brand" href="/runs" aria-label="maestro console home">
        <span className="brand-mark">S</span>
        <span>
          <strong>maestro</strong>
          <small>operator console</small>
        </span>
      </Link>
      <nav aria-label="Primary navigation">
        {SECTIONS.map((section) => (
          <div className="nav-section" key={section.label}>
            <p className="nav-label">{section.label}</p>
            {section.items.map((item) => {
              const Icon = NAV_ICONS[item.icon];
              const active = pathname === item.href || pathname.startsWith(`${item.href}/`);
              return (
                <Link
                  aria-current={active ? "page" : undefined}
                  className={`nav-item${active ? " nav-item-active" : ""}`}
                  href={item.href}
                  key={item.href}
                >
                  <Icon size={17} />
                  <span>{item.label}</span>
                </Link>
              );
            })}
          </div>
        ))}
      </nav>
    </aside>
  );
}
