export default function DashboardLoading() {
  return (
    <div className="space-y-4">
      <div className="rounded-lg border border-border bg-card p-4">
        <div className="h-4 w-24 animate-pulse rounded bg-muted" />
        <div className="mt-3 h-16 animate-pulse rounded bg-muted/70" />
      </div>
      <div className="rounded-lg border border-border bg-card p-4">
        <div className="h-4 w-40 animate-pulse rounded bg-muted" />
        <div className="mt-3 h-[360px] animate-pulse rounded bg-muted/70" />
      </div>
    </div>
  );
}
