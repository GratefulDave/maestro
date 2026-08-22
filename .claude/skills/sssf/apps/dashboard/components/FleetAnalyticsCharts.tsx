"use client";
import type { ReactNode } from "react";

import {
  Bar,
  BarChart,
  Cell,
  Legend,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

export interface FleetChartDatum {
  label: string;
  value: number;
}

const CHART_COLORS = ["#51c987", "#5da5ff", "#f1b64e", "#e66375", "#9b8afb", "#6bd8d3"];

const tooltipStyle = {
  background: "var(--color-panel)",
  border: "1px solid var(--color-border)",
  borderRadius: 8,
  color: "var(--color-ink)",
};

function ChartPanel({
  title,
  description,
  children,
}: {
  title: string;
  description: string;
  children: ReactNode;
}) {
  return (
    <article className="panel section-panel">
      <div className="section-heading">
        <div>
          <h3>{title}</h3>
          <p>{description}</p>
        </div>
      </div>
      <div style={{ height: 260 }}>{children}</div>
    </article>
  );
}

export function FleetAnalyticsCharts({
  liveStateData,
  nodeStateData,
}: {
  liveStateData: FleetChartDatum[];
  nodeStateData: FleetChartDatum[];
}) {
  return (
    <div className="stat-grid">
      <ChartPanel
        title="Run live state"
        description="Derived from node rows, never stored as a run column."
      >
        <ResponsiveContainer>
          <PieChart>
            <Pie
              data={liveStateData}
              dataKey="value"
              nameKey="label"
              innerRadius={48}
              outerRadius={80}
              paddingAngle={2}
            >
              {liveStateData.map((entry, index) => (
                <Cell fill={CHART_COLORS[index % CHART_COLORS.length]} key={entry.label} />
              ))}
            </Pie>
            <Tooltip contentStyle={tooltipStyle} />
            <Legend />
          </PieChart>
        </ResponsiveContainer>
      </ChartPanel>
      <ChartPanel
        title="Node states"
        description="Counts from node_lifecycle across every loaded run."
      >
        <ResponsiveContainer>
          <BarChart data={nodeStateData}>
            <XAxis dataKey="label" stroke="var(--color-muted)" />
            <YAxis allowDecimals={false} stroke="var(--color-muted)" />
            <Tooltip contentStyle={tooltipStyle} />
            <Bar dataKey="value" radius={[4, 4, 0, 0]}>
              {nodeStateData.map((entry, index) => (
                <Cell fill={CHART_COLORS[index % CHART_COLORS.length]} key={entry.label} />
              ))}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </ChartPanel>
    </div>
  );
}
