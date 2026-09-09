"use client";

import useSWR from "swr";
import { useParams } from "next/navigation";
import { DatasetDetailView } from "@/components/dataset-detail-view";
import { ExperimentPageLoadAlert } from "@/components/experiment-page-load-alert";
import { Nav } from "@/components/nav";
import type { PublicExperimentInfo } from "@/lib/types";
import { fetcher } from "@/lib/api";
import { useExperimentResults } from "@/lib/use-experiment-results";
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
    error: openError,
    isLoading,
    isLoadingTrials,
    refreshResults,
    trialsLoaded,
    totalTrials,
  } = useExperimentResults({
    url: publicBase ? `${publicBase}/results` : null,
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
            openError ? (
              <ExperimentPageLoadAlert
                resource={hasFatalError ? "dataset" : "trials"}
                description="The dataset token may be invalid, or this experiment is not public."
                loaded={trialsLoaded}
                total={totalTrials}
                isRetrying={isLoadingTrials}
                onRetry={() => void refreshResults()}
              />
            ) : null
          }
        />
      </main>
    </>
  );
}
