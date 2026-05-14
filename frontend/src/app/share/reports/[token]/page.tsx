"use client";

import Image from "next/image";
import Link from "next/link";
import { useParams } from "next/navigation";
import useSWR from "swr";
import { Loader2 } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { ExperimentDetailView } from "@/components/experiment-detail-view";
import { fetcher } from "@/lib/api";
import type { PublicReport } from "@/lib/types";
import { PUBLIC_API_URL } from "@/lib/utils";

/**
 * Anonymous report view. Deliberately does not mount the logged-in
 * Nav so it doesn't look like an authed dashboard. Reuses
 * ``ExperimentDetailView`` in ``readOnly`` mode; the backend already
 * redacts internal task fields in ``_redact_task_for_public`` and
 * gates trial deep-links by the resolved-cell set.
 */
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

  return (
    <div className="flex min-h-screen flex-col">
      <header className="border-b border-[#6f88b4]/15 bg-card/40">
        <div className="mx-auto flex h-12 max-w-(--breakpoint-2xl) items-center gap-2 px-4">
          <Link
            href="https://www.oddish.app"
            className="flex items-center gap-2 text-sm font-medium text-muted-foreground hover:text-foreground"
          >
            <Image
              src="/oddish.png"
              alt="Oddish"
              width={20}
              height={20}
              className="drop-shadow-xs"
            />
            <span>Oddish</span>
          </Link>
        </div>
      </header>
      <main className="mx-auto w-full max-w-(--breakpoint-2xl) flex-1 px-4 py-4">
        {isLoading ? (
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" /> Loading…
          </div>
        ) : error || !data ? (
          <Alert variant="destructive">
            <AlertTitle>Report not found</AlertTitle>
            <AlertDescription>
              This share link is invalid, expired, or has been unpublished.
            </AlertDescription>
          </Alert>
        ) : (
          <ExperimentDetailView
            tasksForExperiment={data.tasks}
            isLoading={false}
            hasError={false}
            headerLeft={headerLeft}
            readOnly
            allowRetry={false}
            apiBaseUrl={PUBLIC_API_URL}
            creationMeta={{
              createdAt: data.created_at,
              author: data.created_by_display,
            }}
          />
        )}
      </main>
    </div>
  );
}
