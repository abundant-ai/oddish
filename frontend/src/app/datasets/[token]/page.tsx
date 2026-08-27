"use client";

import useSWR from "swr";
import { useParams } from "next/navigation";
import { DatasetDetailView } from "@/components/dataset-detail-view";
import { Nav } from "@/components/nav";
import type { PublicExperimentInfo } from "@/lib/types";
import { fetcher } from "@/lib/api";
import { PUBLIC_API_URL } from "@/lib/utils";
import { useExperimentPageResources } from "@/lib/use-experiment-page";
import { Button } from "@/components/ui/button";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";

export default function PublicDatasetPage() {
  const params = useParams();
  const token = Array.isArray(params.token) ? params.token[0] : params.token;

  const { data: experimentInfo } = useSWR<PublicExperimentInfo>(
    token ? `${PUBLIC_API_URL}/experiments/${token}` : null,
    fetcher
  );

  const resources = useExperimentPageResources({
    kind: "public",
    token: token ?? "",
  });

  const datasetName =
    experimentInfo?.name || resources.open?.name || "Public Dataset";
  const hasError = Boolean(resources.fatalError);

  return (
    <>
      <Nav />

      <main className="mx-auto w-full max-w-(--breakpoint-2xl) px-4 py-4">
        <DatasetDetailView
          datasetName={datasetName}
          tasks={resources.tasks}
          isLoading={resources.isLoading}
          hasError={hasError}
        />
        {resources.openError && resources.open && (
          <Alert className="fixed right-6 bottom-20 w-auto max-w-md">
            <AlertTitle>Some tasks failed to load</AlertTitle>
            <AlertDescription className="flex items-center gap-2">
              <span>Showing the task rows already loaded.</span>
              <Button
                type="button"
                size="sm"
                variant="secondary"
                onClick={() => void resources.retryOpen()}
              >
                Retry
              </Button>
            </AlertDescription>
          </Alert>
        )}
        {(resources.hasMoreTasks || resources.hasMoreTrials) && (
          <div className="fixed right-6 bottom-6 flex gap-2">
            {resources.hasMoreTasks && (
              <Button
                type="button"
                variant="secondary"
                onClick={() => void resources.loadMoreTasks()}
              >
                Load more tasks
              </Button>
            )}
            {resources.hasMoreTrials && (
              <Button
                type="button"
                variant="secondary"
                onClick={() => void resources.loadMoreTrials()}
              >
                Load more trials
              </Button>
            )}
          </div>
        )}
      </main>
    </>
  );
}
