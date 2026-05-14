"use client";

import { useParams } from "next/navigation";
import useSWR from "swr";
import { Loader2 } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { ReportGrid } from "@/components/report-grid";
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

  return (
    <div className="mx-auto max-w-6xl space-y-4 p-6">
      <header className="space-y-1">
        <div className="text-xs uppercase tracking-wide text-muted-foreground">
          Shared report
        </div>
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
          <>
            <h1 className="text-2xl font-semibold">{data.name}</h1>
            {data.description ? (
              <p className="text-sm text-muted-foreground">
                {data.description}
              </p>
            ) : null}
            <div className="text-xs text-muted-foreground">
              Shared on {new Date(data.created_at).toLocaleDateString()}
              {data.created_by_display ? (
                <> by {data.created_by_display}</>
              ) : null}
              {" · "}
              {data.rows.length} × {data.columns.length} grid · {data.total_trials}{" "}
              trials
            </div>
          </>
        )}
      </header>

      {data ? <ReportGrid report={data} /> : null}

      <footer className="pt-6 text-center text-xs text-muted-foreground">
        Generated with{" "}
        <a
          href="/"
          className="text-blue-400 hover:underline"
          rel="noreferrer"
        >
          Oddish
        </a>
      </footer>
    </div>
  );
}
