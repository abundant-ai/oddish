"use client";

import dynamic from "next/dynamic";
import useSWR from "swr";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { fetcher } from "@/lib/api";
import type { Analyzer } from "@/lib/types";

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

function Section({ title, content }: { title: string; content?: string | null }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">{title}</CardTitle>
      </CardHeader>
      <CardContent>
        {content && content.trim() ? (
          <MarkdownRenderer content={content} />
        ) : (
          <div className="text-muted-foreground text-sm">Nothing to analyzer.</div>
        )}
      </CardContent>
    </Card>
  );
}

export function AnalyzerDetailClient({ analyzerId }: { analyzerId: string }) {
  const { data: analyzer, error } = useSWR<Analyzer>(
    `/api/analyzers/${analyzerId}`,
    fetcher,
    {
      refreshInterval: (r) =>
        r && (r.status === "success" || r.status === "failed") ? 0 : 3000,
    },
  );

  if (error) return <div className="text-sm text-red-500">Failed to load analyzer.</div>;
  if (!analyzer) return <div className="text-muted-foreground text-sm">Loading…</div>;

  const generating =
    analyzer.status === "pending" ||
    analyzer.status === "queued" ||
    analyzer.status === "running";

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h1 className="text-xl font-semibold">{analyzer.name}</h1>
          <div className="text-muted-foreground mt-1 flex items-center gap-2 text-xs">
            <Badge variant="outline">{analyzer.status}</Badge>
            <span>{analyzer.num_trials ?? 0} trials</span>
            <span>· {analyzer.num_bad_failures ?? 0} bad</span>
            <span>· {analyzer.num_good_failures ?? 0} good</span>
          </div>
        </div>
        {analyzer.breakdown && (
          <div className="flex flex-wrap gap-1">
            {Object.entries(analyzer.breakdown).map(([code, count]) => (
              <Badge key={code} variant="secondary" className="text-[10px]">
                {(SUBCATEGORY_LABELS[code] ?? code)}: {count}
              </Badge>
            ))}
          </div>
        )}
      </div>

      {generating && (
        <div className="text-muted-foreground text-sm">
          Generating analyzer… this page updates automatically.
        </div>
      )}
      {analyzer.status === "failed" && analyzer.error && (
        <div className="text-sm text-red-500">{analyzer.error}</div>
      )}

      <Section title="Bad failures (reward hacking)" content={analyzer.bad_failure_content} />
      <Section title="Good failures (capability)" content={analyzer.good_failure_content} />
      <Section
        title="Universal capabilities"
        content={analyzer.universal_capabilities_content}
      />
      <Section title="Headroom analysis" content={analyzer.headroom_analysis} />
    </div>
  );
}
