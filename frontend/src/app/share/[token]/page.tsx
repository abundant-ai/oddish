"use client";

import { useParams } from "next/navigation";
import useSWR from "swr";
import { ExperimentDetailView } from "@/components/experiment-detail-view";
import { ExperimentDescription } from "@/components/experiment-description";
import { ExperimentPageLoadAlert } from "@/components/experiment-page-load-alert";
import { ShareNav } from "@/components/share-nav";
import type { PublicExperimentInfo } from "@/lib/types";
import { fetcher } from "@/lib/api";
import { useExperimentResults } from "@/lib/use-experiment-results";
import { useExperimentCostTotals } from "@/lib/use-experiment-cost-totals";
import { PUBLIC_API_URL } from "@/lib/utils";

export default function PublicExperimentPage() {
  const params = useParams();
  const token = Array.isArray(params.token) ? params.token[0] : params.token;
  const publicBase = token
    ? `${PUBLIC_API_URL}/experiments/${encodeURIComponent(token)}`
    : null;

  const { data: experimentInfo } = useSWR<PublicExperimentInfo>(
    publicBase,
    fetcher
  );

  const {
    experiment,
    tasks: tasksForExperiment,
    error: openError,
    isLoading,
    isLoadingTrials,
    trialsLoaded,
    totalTrials,
    pagesComplete,
    refreshResults,
  } = useExperimentResults({
    url: publicBase ? `${publicBase}/results` : null,
    publicView: true,
  });
  const { resource: costTotals, refresh: refreshCostTotals } =
    useExperimentCostTotals({
      url: publicBase ? `${publicBase}/cost-totals` : null,
      hasActiveTrials: experiment?.has_active_trials ?? false,
    });

  const experimentName =
    experimentInfo?.name || experiment?.name || "Public Experiment";
  const hasFatalError = !experiment && Boolean(openError);
  const scopedApiBaseUrl = publicBase ?? PUBLIC_API_URL;

  return (
    <>
      <ShareNav />

      <main className="mx-auto w-full max-w-(--breakpoint-2xl) px-4 py-4">
        <div className="space-y-4">
          <ExperimentDetailView
            tasksForExperiment={tasksForExperiment}
            pageSummary={experiment?.summary ?? undefined}
            costTotals={costTotals}
            onRetryCostTotals={() => void refreshCostTotals()}
            isLoading={isLoading}
            isLoadingTrials={isLoadingTrials}
            pagesComplete={pagesComplete}
            focusUrl={publicBase ? `${publicBase}/focus` : undefined}
            hasError={hasFatalError}
            errorTitle="Failed to load experiment"
            errorDescription="The share link may be invalid or no longer public."
            inlineAlert={
              openError ? (
                <ExperimentPageLoadAlert
                  resource="trials"
                  loaded={trialsLoaded}
                  total={totalTrials}
                  isRetrying={isLoadingTrials}
                  onRetry={() => void refreshResults()}
                />
              ) : null
            }
            headerStatus={
              isLoadingTrials ? (
                <span role="status" className="text-muted-foreground text-xs">
                  Loading trials {trialsLoaded}/{totalTrials}…
                </span>
              ) : null
            }
            headerLeft={
              <h1 className="truncate pb-1 font-mono text-[26px] leading-[1.25] font-semibold tracking-[-0.02em] text-[color:var(--paper-ink)]">
                {experimentName}
              </h1>
            }
            headerDescription={
              <ExperimentDescription
                description={experimentInfo?.description ?? null}
                readOnly
              />
            }
            readOnly
            allowRetry={false}
            showAnalysis={false}
            apiBaseUrl={scopedApiBaseUrl}
          />
        </div>
      </main>
    </>
  );
}
