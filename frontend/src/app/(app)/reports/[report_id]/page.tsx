"use client";

import { useParams } from "next/navigation";
import { useState } from "react";
import useSWR from "swr";
import { Loader2, Share2, Trash2 } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardHeader } from "@/components/ui/card";
import { ReportBackfillBanner } from "@/components/report-backfill-banner";
import { ReportBackfillHistory } from "@/components/report-backfill-history";
import { ReportGrid } from "@/components/report-grid";
import { fetcher } from "@/lib/api";
import type { Report, ReportShareResponse } from "@/lib/types";
import { useRouter } from "next/navigation";

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

  if (isLoading) {
    return (
      <div className="flex items-center gap-2 p-4 text-sm text-muted-foreground">
        <Loader2 className="h-4 w-4 animate-spin" /> Loading…
      </div>
    );
  }
  if (error) {
    return (
      <Alert variant="destructive">
        <AlertTitle>Failed to load report</AlertTitle>
        <AlertDescription>
          {error instanceof Error ? error.message : "Unknown error"}
        </AlertDescription>
      </Alert>
    );
  }
  if (!data) return null;

  async function toggleShare(publish: boolean) {
    if (!reportId) return;
    setShareBusy(true);
    try {
      const res = await fetch(`/api/reports/${reportId}/share`, {
        method: publish ? "POST" : "DELETE",
        credentials: "include",
      });
      const body = (await res.json()) as ReportShareResponse;
      if (res.ok && data) {
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

  const sharedUrl =
    data.is_public && data.public_token
      ? `${typeof window !== "undefined" ? window.location.origin : ""}/share/reports/${data.public_token}`
      : null;

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader className="py-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div>
              <h1 className="text-base font-semibold">{data.name}</h1>
              {data.description ? (
                <p className="text-sm text-muted-foreground">
                  {data.description}
                </p>
              ) : null}
              <div className="text-xs text-muted-foreground">
                {data.rows.length} task version(s) × {data.columns.length}{" "}
                agent(s) · {data.total_trials} trials
                {data.total_missing > 0 ? (
                  <span className="ml-1 text-yellow-400">
                    · {data.total_missing} missing
                  </span>
                ) : null}
              </div>
            </div>
            <div className="flex items-center gap-2">
              {data.is_public ? (
                <>
                  <span className="text-xs text-muted-foreground">
                    {sharedUrl}
                  </span>
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
                  {shareBusy ? "…" : "Publish"}
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
          </div>
        </CardHeader>
      </Card>

      <ReportBackfillBanner
        reportId={data.id}
        onSubmitted={() => mutate()}
      />

      <ReportGrid report={data} />

      <ReportBackfillHistory reportId={data.id} />
    </div>
  );
}
