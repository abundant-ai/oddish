"use client";

import { useParams, useRouter } from "next/navigation";
import { useState } from "react";
import useSWR from "swr";
import { Loader2, Share2, Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { ExperimentDetailView } from "@/components/experiment-detail-view";
import { ReportBackfillBanner } from "@/components/report-backfill-banner";
import { ReportBackfillHistory } from "@/components/report-backfill-history";
import { ReportViewSpecDialog } from "@/components/report-view-spec-dialog";
import { fetcher } from "@/lib/api";
import type { Report, ReportShareResponse } from "@/lib/types";

export default function ReportDetailPage() {
  const params = useParams();
  const router = useRouter();
  const reportId = Array.isArray(params.report_id)
    ? params.report_id[0]
    : params.report_id;

  const { data, error, isLoading, mutate } = useSWR<Report>(
    reportId ? `/api/reports/${reportId}` : null,
    fetcher,
    { refreshInterval: 15_000 },
  );

  const [shareBusy, setShareBusy] = useState(false);
  const [deleteBusy, setDeleteBusy] = useState(false);

  async function toggleShare(publish: boolean) {
    if (!reportId || !data) return;
    setShareBusy(true);
    try {
      const res = await fetch(`/api/reports/${reportId}/share`, {
        method: publish ? "POST" : "DELETE",
        credentials: "include",
      });
      const body = (await res.json()) as ReportShareResponse;
      if (res.ok) {
        await mutate({
          ...data,
          is_public: body.is_public,
          public_token: body.public_token,
        });
      }
    } finally {
      setShareBusy(false);
    }
  }

  async function deleteReport() {
    if (!reportId) return;
    if (!confirm("Delete this report? Trials and audit history are preserved."))
      return;
    setDeleteBusy(true);
    try {
      const res = await fetch(`/api/reports/${reportId}`, {
        method: "DELETE",
        credentials: "include",
      });
      if (res.ok) router.push("/reports");
    } finally {
      setDeleteBusy(false);
    }
  }

  const headerLeft = (
    <div className="space-y-1">
      <h1 className="truncate pb-1 font-mono text-[26px] font-semibold leading-[1.25] tracking-[-0.02em] text-[color:var(--paper-ink)]">
        {data?.name ?? "Report"}
      </h1>
      {data?.description ? (
        <p className="text-sm text-muted-foreground">{data.description}</p>
      ) : null}
      {data ? (
        <p className="text-xs text-muted-foreground">
          {data.tasks.length} task version(s) × {data.columns.length} agent(s) ·{" "}
          {data.total_trials} trials
          {data.total_missing > 0 ? (
            <span className="ml-1 text-yellow-500">
              · {data.total_missing} missing
            </span>
          ) : null}
        </p>
      ) : null}
    </div>
  );

  const sharedUrl =
    data?.is_public && data.public_token
      ? `${typeof window !== "undefined" ? window.location.origin : ""}/share/reports/${data.public_token}`
      : null;

  const headerRight = data ? (
    <div className="flex items-center gap-2">
      <ReportViewSpecDialog reportId={data.id} />
      {data.is_public ? (
        <>
          {sharedUrl ? (
            <a
              href={sharedUrl}
              className="hidden text-xs text-blue-400 hover:underline md:block"
              target="_blank"
              rel="noreferrer"
            >
              {sharedUrl}
            </a>
          ) : null}
          <Button
            size="sm"
            variant="outline"
            disabled={shareBusy}
            onClick={() => toggleShare(false)}
          >
            <Share2 className="mr-2 h-3 w-3" />
            Unpublish
          </Button>
        </>
      ) : (
        <Button
          size="sm"
          disabled={shareBusy}
          onClick={() => toggleShare(true)}
        >
          <Share2 className="mr-2 h-3 w-3" />
          {shareBusy ? "Publishing…" : "Publish"}
        </Button>
      )}
      <Button
        size="sm"
        variant="ghost"
        disabled={deleteBusy}
        onClick={deleteReport}
      >
        <Trash2 className="mr-2 h-3 w-3" />
        Delete
      </Button>
    </div>
  ) : null;

  // ``inlineAlert`` slot in ExperimentDetailView sits between the header
  // and the trials table — perfect placement for the backfill banner +
  // audit history without forking the layout.
  const inlineAlert = reportId ? (
    <div className="space-y-3">
      <ReportBackfillBanner reportId={reportId} onSubmitted={() => mutate()} />
      <ReportBackfillHistory reportId={reportId} />
    </div>
  ) : null;

  if (isLoading) {
    return (
      <div className="flex items-center gap-2 p-6 text-sm text-muted-foreground">
        <Loader2 className="h-4 w-4 animate-spin" /> Loading…
      </div>
    );
  }

  return (
    <ExperimentDetailView
      tasksForExperiment={data?.tasks ?? []}
      isLoading={isLoading}
      hasError={Boolean(error)}
      errorTitle="Failed to load report"
      errorDescription={
        error instanceof Error ? error.message : "Unknown error"
      }
      headerLeft={headerLeft}
      headerRight={headerRight}
      inlineAlert={inlineAlert}
      allowRetry={false}
      readOnly={false}
    />
  );
}
