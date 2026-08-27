"use client";

import { useParams } from "next/navigation";
import useSWR from "swr";
import { ExperimentDetailView } from "@/components/experiment-detail-view";
import { ExperimentDescription } from "@/components/experiment-description";
import { ShareNav } from "@/components/share-nav";
import type { PublicExperimentInfo } from "@/lib/types";
import { fetcher } from "@/lib/api";
import { PUBLIC_API_URL } from "@/lib/utils";
import { useExperimentPageResources } from "@/lib/use-experiment-page";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { ExperimentPageSkeleton } from "@/components/experiment-page-skeleton";

export default function PublicExperimentPage() {
  const params = useParams();
  const token = Array.isArray(params.token) ? params.token[0] : params.token;

  const { data: experimentInfo, error: experimentError } =
    useSWR<PublicExperimentInfo>(
      token ? `${PUBLIC_API_URL}/experiments/${token}` : null,
      fetcher
    );

  const resources = useExperimentPageResources({
    kind: "public",
    token: token ?? "",
  });

  const experimentName =
    experimentInfo?.name || resources.open?.name || "Public Experiment";
  const hasErrors = Boolean(resources.fatalError);
  const scopedApiBaseUrl = token
    ? `${PUBLIC_API_URL}/experiments/${encodeURIComponent(token)}`
    : PUBLIC_API_URL;

  return (
    <>
      <ShareNav />

      <main className="mx-auto w-full max-w-(--breakpoint-2xl) px-4 py-4">
        <div className="space-y-4">
          {resources.open ? (
            <ExperimentDetailView
              experimentCreatedAt={resources.open.created_at}
              experimentOwner={resources.open.owner}
              experimentLink={resources.open.link}
              tasksForExperiment={resources.tasks}
              exactSummary={resources.open.summary}
              incompleteTaskIds={resources.incompleteTaskIds}
              isLoading={resources.isLoading}
              isLoadingTrials={resources.isLoadingTrials}
              hasError={hasErrors}
              errorTitle="Failed to load experiment"
              errorDescription="The share link may be invalid or no longer public."
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
              inlineAlert={
                resources.openError ? (
                  <Alert>
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
                ) : resources.trialError ? (
                  <Alert variant="destructive">
                    <AlertTitle>Some trial results failed to load</AlertTitle>
                    <AlertDescription className="flex items-center gap-2">
                      <span>
                        Loaded {resources.loadedTrialCount}/
                        {resources.open?.summary.trial_count ?? 0} trials.
                      </span>
                      <Button
                        type="button"
                        size="sm"
                        variant="secondary"
                        onClick={() => void resources.retryTrials()}
                      >
                        Retry
                      </Button>
                    </AlertDescription>
                  </Alert>
                ) : experimentError ? (
                  <Alert>
                    <AlertTitle>Description unavailable</AlertTitle>
                    <AlertDescription>
                      The experiment results loaded, but its description did
                      not.
                    </AlertDescription>
                  </Alert>
                ) : resources.hasMoreTasks || resources.hasMoreTrials ? (
                  <div className="flex gap-2">
                    {resources.hasMoreTasks && (
                      <Button
                        type="button"
                        size="sm"
                        variant="secondary"
                        onClick={() => void resources.loadMoreTasks()}
                      >
                        Load more tasks
                      </Button>
                    )}
                    {resources.hasMoreTrials && (
                      <Button
                        type="button"
                        size="sm"
                        variant="secondary"
                        onClick={() => void resources.loadMoreTrials()}
                      >
                        Load more trials
                      </Button>
                    )}
                  </div>
                ) : null
              }
              readOnly
              allowRetry={false}
              showAnalysis={false}
              apiBaseUrl={scopedApiBaseUrl}
            />
          ) : resources.fatalError ? (
            <Alert variant="destructive">
              <AlertTitle>Failed to load experiment</AlertTitle>
              <AlertDescription>
                The share link may be invalid or no longer public.
              </AlertDescription>
            </Alert>
          ) : (
            <ExperimentPageSkeleton />
          )}
        </div>
      </main>
    </>
  );
}
