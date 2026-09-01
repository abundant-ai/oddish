"use client";

import useSWR from "swr";
import { useParams } from "next/navigation";
import { DatasetDetailView } from "@/components/dataset-detail-view";
import { ExperimentPaginationSentinel } from "@/components/experiment-pagination-sentinel";
import { ExperimentTrialLoadAlert } from "@/components/experiment-trial-load-alert";
import { Nav } from "@/components/nav";
import type { PublicExperimentInfo } from "@/lib/types";
import { fetcher } from "@/lib/api";
import { useExperimentPages } from "@/lib/use-experiment-pages";
import { PUBLIC_API_URL } from "@/lib/utils";

export default function PublicDatasetPage() {
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
    tasks,
    openError,
    isLoading,
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

  const datasetName =
    experimentInfo?.name || experiment?.name || "Public Dataset";
  const hasFatalError = !experiment && Boolean(openError);

  return (
    <>
      <Nav />

      <main className="mx-auto w-full max-w-(--breakpoint-2xl) px-4 py-4">
        <DatasetDetailView
          datasetName={datasetName}
          tasks={tasks}
          isLoading={isLoading}
          hasError={hasFatalError}
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
        />
        <ExperimentPaginationSentinel
          hasMoreTasks={hasMoreTasks}
          hasMoreTrials={hasMoreTrials}
          loadNextTasks={loadNextTasks}
          loadNextTrials={loadNextTrials}
        />
      </main>
    </>
  );
}
