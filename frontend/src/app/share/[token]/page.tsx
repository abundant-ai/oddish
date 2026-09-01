"use client";

import { useParams } from "next/navigation";
import useSWR from "swr";
import { ExperimentDetailView } from "@/components/experiment-detail-view";
import { ExperimentDescription } from "@/components/experiment-description";
import { ExperimentTrialLoadAlert } from "@/components/experiment-trial-load-alert";
import { ShareNav } from "@/components/share-nav";
import type { PublicExperimentInfo } from "@/lib/types";
import { fetcher } from "@/lib/api";
import { useExperimentPages } from "@/lib/use-experiment-pages";
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
    openError,
    isLoading,
    isLoadingTrials,
    hasMoreTasks,
    hasMoreTrials,
    loadNextTasks,
    loadNextTrials,
    trialsLoaded,
    totalTrials,
    trialsStalled,
    isValidatingTrials,
  } = useExperimentPages({
    openUrl: publicBase ? `${publicBase}/open` : null,
    trialPageUrl: publicBase ? `${publicBase}/trial-page` : null,
    publicView: true,
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
            pageSummary={experiment?.summary}
            isLoading={isLoading}
            isLoadingTrials={isLoadingTrials}
            hasMoreTasks={hasMoreTasks}
            hasMoreTrials={hasMoreTrials}
            loadNextTasks={loadNextTasks}
            loadNextTrials={loadNextTrials}
            hasError={hasFatalError}
            errorTitle="Failed to load experiment"
            errorDescription="The share link may be invalid or no longer public."
            inlineAlert={
              trialsStalled ? (
                <ExperimentTrialLoadAlert
                  loaded={trialsLoaded}
                  total={totalTrials}
                  isRetrying={isValidatingTrials}
                  onRetry={loadNextTrials}
                />
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
