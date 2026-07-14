"use client";

import dynamic from "next/dynamic";
import useSWR from "swr";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { fetcher } from "@/lib/api";
import type { Report } from "@/lib/types";

const MarkdownRenderer = dynamic(() =>
  import("@/components/renderers/markdown-renderer").then(
    (mod) => mod.MarkdownRenderer,
  ),
);

const SUBCATEGORY_LABELS: Record<string, string> = {
  "1a": "Task ambiguity / spec",
  "1b": "Task security / construction",
  "3a": "Problem identification",
  "3b": "Implementation",
  "3c": "Syntax",
  emergent: "Emergent",
};

function Section({
  title,
  content,
  generating,
}: {
  title: string;
  content?: string | null;
  generating?: boolean;
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">{title}</CardTitle>
      </CardHeader>
      <CardContent>
        {content && content.trim() ? (
          <MarkdownRenderer content={content} />
        ) : (
          <div className="text-muted-foreground text-sm">
            {generating ? "Generating…" : "No findings for this section."}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

export function ReportDetailClient({ reportId }: { reportId: string }) {
  const { data: report, error } = useSWR<Report>(
    `/api/reports/${reportId}`,
    fetcher,
    {
      refreshInterval: (r) =>
        r && (r.status === "success" || r.status === "failed") ? 0 : 3000,
    },
  );

  if (error) return <div className="text-sm text-red-500">Failed to load report.</div>;
  if (!report) return <div className="text-muted-foreground text-sm">Loading…</div>;

  const generating =
    report.status === "pending" ||
    report.status === "queued" ||
    report.status === "running";

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h1 className="text-xl font-semibold">{report.name}</h1>
          <div className="text-muted-foreground mt-1 flex items-center gap-2 text-xs">
            <Badge variant="outline">{report.status}</Badge>
            {generating ? (
              <span>counting trials…</span>
            ) : (
              <>
                <span>{report.num_trials ?? 0} trials</span>
                <span>· {report.num_bad_failures ?? 0} bad</span>
                <span>· {report.num_good_failures ?? 0} good</span>
              </>
            )}
          </div>
        </div>
        {report.breakdown && (
          <div className="flex flex-wrap gap-1">
            {Object.entries(report.breakdown).map(([code, count]) => (
              <Badge key={code} variant="secondary" className="text-[10px]">
                {(SUBCATEGORY_LABELS[code] ?? code)}: {count}
              </Badge>
            ))}
          </div>
        )}
      </div>

      {generating && (
        <div className="text-muted-foreground text-sm">
          Generating report… this page updates automatically.
        </div>
      )}
      {report.status === "failed" && report.error && (
        <div className="text-sm text-red-500">{report.error}</div>
      )}

      <Section
        title="Bad failures (reward hacking)"
        content={report.bad_failure_content}
        generating={generating}
      />
      <Section
        title="Good failures (capability)"
        content={report.good_failure_content}
        generating={generating}
      />
      <Section
        title="Universal capabilities"
        content={report.universal_capabilities_content}
        generating={generating}
      />
      <Section
        title="Headroom analysis"
        content={report.headroom_analysis}
        generating={generating}
      />
    </div>
  );
}
