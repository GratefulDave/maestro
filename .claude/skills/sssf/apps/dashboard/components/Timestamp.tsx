export function Timestamp({
  value,
  fallback = "—",
}: {
  value?: string | number | Date | null;
  fallback?: string;
}) {
  if (value === null || value === undefined || value === "") return <span>{fallback}</span>;
  const date = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(date.getTime())) return <span>{fallback}</span>;
  return (
    <time dateTime={date.toISOString()} title={date.toLocaleString()}>
      {new Intl.DateTimeFormat("en", {
        month: "short",
        day: "numeric",
        hour: "numeric",
        minute: "2-digit",
      }).format(date)}
    </time>
  );
}
