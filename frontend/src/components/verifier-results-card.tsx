"use client";

import { useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  CircleSlash,
  ExternalLink,
  Loader2,
  XCircle,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { Trial } from "@/lib/types";
import { cn } from "@/lib/utils";
import {
  embeddedCtrfSummary,
  parseCtrfReport,
  verifierMetrics,
  type CtrfSummary,
} from "@/lib/verifier-results";

interface TrialFile {
  path?: string;
}

interface ArtifactCtrfState {
  key: string;
  status: "loading" | "done";
  summary: CtrfSummary | null;
  reportPath: string | null;
}

interface VerifierResultsCardProps {
  trial: Trial;
  apiBaseUrl: string;
  enabled: boolean;
  onViewFile: (path: string) => void;
}

function trialFilesBase(apiBaseUrl: string, trialId: string): string {
  return `${apiBaseUrl.replace(/\/$/, "")}/trials/${encodeURIComponent(trialId)}/files`;
}

function encodeFilePath(path: string): string {
  return path.split("/").map(encodeURIComponent).join("/");
}

function hasTerminalStatus(trial: Trial): boolean {
  return ["success", "failed", "cancelled", "skipped"].includes(trial.status);
}

function CtrfProgress({ summary }: { summary: CtrfSummary }) {
  if (summary.tests <= 0) return null;
  const segments = [
    { key: "passed", count: summary.passed, className: "bg-emerald-500" },
    { key: "failed", count: summary.failed, className: "bg-red-500" },
    { key: "skipped", count: summary.skipped, className: "bg-slate-400" },
    { key: "pending", count: summary.pending, className: "bg-amber-400" },
    { key: "other", count: summary.other, className: "bg-violet-400" },
  ];

  return (
    <div
      className="bg-muted mt-2 flex h-1.5 overflow-hidden rounded-full"
      aria-label={`${summary.passed} of ${summary.tests} checks passed`}
    >
      {segments.map((segment) =>
        segment.count > 0 ? (
          <span
            key={segment.key}
            className={segment.className}
            style={{ width: `${(segment.count / summary.tests) * 100}%` }}
          />
        ) : null,
      )}
    </div>
  );
}

export function VerifierResultsCard({
  trial,
  apiBaseUrl,
  enabled,
  onViewFile,
}: VerifierResultsCardProps) {
  const embeddedSummary = useMemo(
    () => embeddedCtrfSummary(trial.result),
    [trial.result],
  );
  const hasEmbeddedSummary = embeddedSummary !== null;
  const isTerminal = hasTerminalStatus(trial);
  const trialId = trial.id;
  const shouldLoadArtifact = enabled && isTerminal && !hasEmbeddedSummary;
  const artifactKey = `${apiBaseUrl}\0${trialId}\0${shouldLoadArtifact ? "load" : "idle"}`;
  const [artifactState, setArtifactState] = useState<ArtifactCtrfState>(() => ({
    key: artifactKey,
    status: shouldLoadArtifact ? "loading" : "done",
    summary: null,
    reportPath: null,
  }));
  const isCurrentArtifact = artifactState.key === artifactKey;
  const artifactSummary = isCurrentArtifact ? artifactState.summary : null;
  const artifactReportPath = isCurrentArtifact
    ? artifactState.reportPath
    : null;
  const isArtifactLoading =
    shouldLoadArtifact &&
    (!isCurrentArtifact || artifactState.status === "loading");

  useEffect(() => {
    setArtifactState({
      key: artifactKey,
      status: shouldLoadArtifact ? "loading" : "done",
      summary: null,
      reportPath: null,
    });
    if (!shouldLoadArtifact) return;

    const controller = new AbortController();
    const filesBase = trialFilesBase(apiBaseUrl, trialId);

    async function loadHistoricalCtrf() {
      let reportPath: string | null = null;
      let summary: CtrfSummary | null = null;
      try {
        const listingResponse = await fetch(
          `${filesBase}?recursive=true&presign=false&limit=1000`,
          { signal: controller.signal },
        );
        if (!listingResponse.ok) return;
        const listing = (await listingResponse.json()) as {
          files?: TrialFile[];
        };
        reportPath =
          listing.files
            ?.map((file) => file.path)
            .find(
              (path): path is string =>
                typeof path === "string" &&
                (path === "verifier/ctrf.json" ||
                  path.endsWith("/verifier/ctrf.json")),
            ) ?? null;
        if (!reportPath) return;

        const reportResponse = await fetch(
          `${filesBase}/${encodeFilePath(reportPath)}`,
          { signal: controller.signal },
        );
        if (!reportResponse.ok) return;
        summary = parseCtrfReport(await reportResponse.json());
      } catch (error) {
        if (!(error instanceof DOMException && error.name === "AbortError")) {
          // CTRF is optional. Reward and task metrics remain the fallback.
        }
      } finally {
        if (!controller.signal.aborted) {
          setArtifactState({
            key: artifactKey,
            status: "done",
            summary,
            reportPath,
          });
        }
      }
    }

    void loadHistoricalCtrf();
    return () => controller.abort();
  }, [apiBaseUrl, artifactKey, shouldLoadArtifact, trialId]);

  const summary = embeddedSummary ?? artifactSummary;
  const isHistoricalSummaryLoading = !hasEmbeddedSummary && isArtifactLoading;
  const metrics = useMemo(() => verifierMetrics(trial.result), [trial.result]);
  const isVerifierRunning =
    ["running", "retrying"].includes(trial.status) &&
    trial.harbor_stage === "verification";
  const isLoading = isVerifierRunning || isHistoricalSummaryLoading;

  if (
    !summary &&
    metrics.length === 0 &&
    trial.reward == null &&
    !isVerifierRunning &&
    !isTerminal
  ) {
    return null;
  }

  const tone = isLoading
    ? "border-blue-500/30 bg-blue-500/5"
    : trial.reward === 1
      ? "border-emerald-500/30 bg-emerald-500/5"
      : trial.reward === 0
        ? "border-red-500/30 bg-red-500/5"
        : trial.reward != null
          ? "border-amber-500/30 bg-amber-500/5"
          : "border-slate-500/30 bg-slate-500/5";

  const ResultIcon = isLoading
    ? Loader2
    : trial.reward === 1
      ? CheckCircle2
      : trial.reward === 0
        ? XCircle
        : trial.reward != null
          ? AlertTriangle
          : CircleSlash;

  let fallbackHeadline = "Not scored";
  let fallbackDetail = "The verifier was disabled or returned no score.";
  if (isHistoricalSummaryLoading) {
    fallbackHeadline = "Loading…";
    fallbackDetail = "Loading the verifier test report.";
  } else if (isVerifierRunning) {
    fallbackHeadline = "Running…";
    fallbackDetail = "The verifier is evaluating this trial.";
  } else if (trial.reward === 1) {
    fallbackHeadline = "Passed";
    fallbackDetail = "The verifier awarded full credit.";
  } else if (trial.reward === 0) {
    fallbackHeadline = "Failed";
    fallbackDetail = "The verifier awarded no credit.";
  } else if (trial.reward != null) {
    fallbackHeadline = `${(trial.reward * 100).toFixed(1)}%`;
    fallbackDetail = "The verifier awarded partial credit.";
  } else if (trial.status === "failed") {
    fallbackHeadline = "No result";
    fallbackDetail = "The verifier did not return a score.";
  }

  const reportPath = embeddedSummary?.reportPath ?? artifactReportPath;

  return (
    <Card className={tone}>
      <CardHeader className="px-4 pt-3 pb-1">
        <CardTitle className="text-muted-foreground flex items-center justify-between gap-3 text-[11px] font-semibold tracking-wider uppercase">
          <span>Verifier Results</span>
          {summary?.tool ? (
            <span className="font-mono text-[10px] font-normal tracking-normal normal-case">
              {summary.tool}
            </span>
          ) : null}
        </CardTitle>
      </CardHeader>
      <CardContent className="px-4 pt-1 pb-3">
        <div className="flex items-start gap-3">
          <ResultIcon
            className={cn(
              "mt-0.5 h-5 w-5 shrink-0",
              isLoading
                ? "animate-spin text-blue-500"
                : trial.reward === 1
                  ? "text-emerald-500"
                  : trial.reward === 0
                    ? "text-red-500"
                    : trial.reward != null
                      ? "text-amber-500"
                      : "text-slate-500",
            )}
          />
          <div className="min-w-0 flex-1">
            {summary ? (
              <>
                <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
                  <span className="font-mono text-lg leading-none font-bold tabular-nums">
                    {summary.passed} of {summary.tests}
                  </span>
                  <span className="text-muted-foreground text-xs">
                    {summary.tests === 1 ? "check" : "checks"} passed
                  </span>
                  {summary.tests > 0 ? (
                    <span className="text-muted-foreground ml-auto font-mono text-xs tabular-nums">
                      {((summary.passed / summary.tests) * 100).toFixed(0)}%
                    </span>
                  ) : null}
                </div>
                <CtrfProgress summary={summary} />
                <div className="text-muted-foreground mt-2 flex flex-wrap gap-x-3 gap-y-1 text-[11px]">
                  <span className="text-emerald-600 dark:text-emerald-400">
                    {summary.passed} passed
                  </span>
                  {summary.failed > 0 ? (
                    <span className="text-red-600 dark:text-red-400">
                      {summary.failed} failed
                    </span>
                  ) : null}
                  {summary.skipped > 0 ? (
                    <span>{summary.skipped} skipped</span>
                  ) : null}
                  {summary.pending > 0 ? (
                    <span>{summary.pending} pending</span>
                  ) : null}
                  {summary.other > 0 ? (
                    <span>{summary.other} other</span>
                  ) : null}
                </div>
              </>
            ) : (
              <>
                <div className="font-mono text-sm font-bold">
                  {fallbackHeadline}
                </div>
                <p className="text-muted-foreground mt-1 text-xs">
                  {fallbackDetail}
                </p>
              </>
            )}

            {metrics.length > 0 ? (
              <div className="mt-3 grid grid-cols-2 gap-1.5 sm:grid-cols-3">
                {metrics.map((metric) => (
                  <div
                    key={metric.key}
                    className="border-border/70 bg-background/55 rounded border px-2 py-1.5"
                  >
                    <div className="text-muted-foreground truncate text-[9px] tracking-wide uppercase">
                      {metric.label}
                    </div>
                    <div className="mt-0.5 truncate font-mono text-xs font-semibold tabular-nums">
                      {metric.value}
                    </div>
                  </div>
                ))}
              </div>
            ) : null}

            <Button
              type="button"
              variant="ghost"
              size="sm"
              className="text-muted-foreground mt-1.5 -ml-2 h-6 px-2 text-[11px]"
              disabled={isArtifactLoading}
              onClick={() =>
                onViewFile(reportPath ?? "verifier/test-stdout.txt")
              }
            >
              {isArtifactLoading
                ? "Finding test report…"
                : reportPath
                  ? "View test report"
                  : "View verifier output"}
              {isArtifactLoading ? (
                <Loader2 className="ml-1 h-3 w-3 animate-spin" />
              ) : (
                <ExternalLink className="ml-1 h-3 w-3" />
              )}
            </Button>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
