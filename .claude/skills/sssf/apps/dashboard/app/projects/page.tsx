import { ApiUnavailable } from "@/components/ApiUnavailable";
import { EmptyState } from "@/components/EmptyState";
import { PageHeader } from "@/components/PageHeader";
import { SourceBanner } from "@/components/SourceBanner";
import { StatCard } from "@/components/StatCard";
import { getSources } from "@/lib/api";

export const dynamic = "force-dynamic";

export default async function ProjectsPage() {
  const sources = await getSources();
  if (!sources.ok) {
    return (
      <section className="page-stack">
        <PageHeader eyebrow="Fleet" title="Projects" description="Registered sources from GET /api/sources." />
        <ApiUnavailable message={sources.message} />
      </section>
    );
  }

  const maestro = sources.data.filter((source) => source.kind === "maestro");

  return (
    <section className="page-stack">
      <PageHeader
        eyebrow="Fleet"
        title="Projects"
        description="Each source is a registered installation. Maestro ledgers are the ones this console can open as runs."
      />
      <SourceBanner
        label={`${sources.data.length} source${sources.data.length === 1 ? "" : "s"}`}
        detail={`${maestro.length} maestro`}
      />
      <div className="stat-grid">
        <StatCard label="Sources" value={sources.data.length} />
        <StatCard label="Maestro" value={maestro.length} />
      </div>
      {sources.data.length === 0 ? (
        <EmptyState
          title="No sources registered"
          description="GET /api/sources returned an empty list."
        />
      ) : (
        <section className="panel section-panel">
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Id</th>
                  <th>Kind</th>
                  <th>Label</th>
                  <th>Count</th>
                  <th>Journal</th>
                  <th>Path</th>
                </tr>
              </thead>
              <tbody>
                {sources.data.map((source) => (
                  <tr key={source.id}>
                    <td>
                      <code>{source.id}</code>
                    </td>
                    <td>{source.kind}</td>
                    <td>{source.label}</td>
                    <td>{source.count}</td>
                    <td>{source.journal_mode}</td>
                    <td>
                      <code>{source.path}</code>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
    </section>
  );
}
