"use client";

import useSWR from "swr";
import { useParams } from "next/navigation";
import { DatasetDetailView } from "@/components/dataset-detail-view";
import { ExperimentPageLoadAlert } from "@/components/experiment-page-load-alert";
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
    retryTrials,
    trialsLoaded,
    totalTrials,
    trialsStalled,
    isValidatingTrials,
    isValidatingOpen,
    mutateOpen,
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
              <ExperimentPageLoadAlert
                resource="trials"
                loaded={trialsLoaded}
                total={totalTrials}
                isRetrying={isValidatingTrials}
                onRetry={retryTrials}
              />
            ) : openError && experiment ? (
              <ExperimentPageLoadAlert
                resource="tasks"
                loaded={tasks.length}
                total={experiment.summary?.task_count ?? 0}
                isRetrying={isValidatingOpen}
                onRetry={() => void mutateOpen()}
              />
            ) : null
          }
        />
      </main>
    </>
  );
}
