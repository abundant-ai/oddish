"use client";

import { useParams } from "next/navigation";
import useSWR from "swr";
import { Loader2 } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { ExperimentDetailView } from "@/components/experiment-detail-view";
import { Nav } from "@/components/nav";
import { fetcher } from "@/lib/api";
import type { PublicReport } from "@/lib/types";
import { PUBLIC_API_URL } from "@/lib/utils";

export default function PublicReportPage() {
  const params = useParams();
  const token = Array.isArray(params.token) ? params.token[0] : params.token;

  const { data, error, isLoading } = useSWR<PublicReport>(
    token ? `${PUBLIC_API_URL}/reports/${token}` : null,
    fetcher,
    { refreshInterval: 30_000 },
  );

  const headerLeft = (
    <div className="space-y-1">
      <div className="text-xs uppercase tracking-wide text-muted-foreground">
        Shared report
      </div>
      <h1 className="truncate pb-1 font-mono text-[26px] font-semibold leading-[1.25] tracking-[-0.02em] text-[color:var(--paper-ink)]">
        {data?.name ?? "Report"}
      </h1>
      {data?.description ? (
        <p className="text-sm text-muted-foreground">{data.description}</p>
      ) : null}
      {data ? (
        <p className="text-xs text-muted-foreground">
          {data.tasks.length} task version(s) × {data.columns.length} agent(s)
          · {data.total_trials} trials · shared{" "}
          {new Date(data.created_at).toLocaleDateString()}
          {data.created_by_display ? (
            <> by {data.created_by_display}</>
          ) : null}
        </p>
      ) : null}
    </div>
  );

  if (isLoading) {
    return (
      <>
        <Nav />
        <main className="mx-auto w-full max-w-(--breakpoint-2xl) px-4 py-4">
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" /> Loading…
          </div>
        </main>
      </>
    );
  }

  if (error || !data) {
    return (
      <>
        <Nav />
        <main className="mx-auto w-full max-w-(--breakpoint-2xl) px-4 py-4">
          <Alert variant="destructive">
            <AlertTitle>Report not found</AlertTitle>
            <AlertDescription>
              This share link is invalid, expired, or has been unpublished.
            </AlertDescription>
          </Alert>
        </main>
      </>
    );
  }

  return (
    <>
      <Nav />
      <main className="mx-auto w-full max-w-(--breakpoint-2xl) px-4 py-4">
        <ExperimentDetailView
          tasksForExperiment={data.tasks}
          isLoading={false}
          hasError={false}
          headerLeft={headerLeft}
          readOnly
          allowRetry={false}
          apiBaseUrl={PUBLIC_API_URL}
        />
      </main>
    </>
  );
}
