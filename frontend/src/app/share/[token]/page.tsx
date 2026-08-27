"use client";

import { useMemo } from "react";
import { useParams } from "next/navigation";
import useSWR from "swr";
import { ExperimentDetailView } from "@/components/experiment-detail-view";
import { ExperimentDescription } from "@/components/experiment-description";
import { ExperimentSectionTabs } from "@/components/experiment-qa/experiment-section-tabs";
import { ShareNav } from "@/components/share-nav";
import type { Task, PublicExperimentInfo } from "@/lib/types";
import { fetcher } from "@/lib/api";
import { preparePublicExperimentTasks } from "@/lib/public-experiment-tasks";
import { buildPublicQaHref } from "@/lib/experiment-qa";
import { PUBLIC_API_URL } from "@/lib/utils";

export default function PublicExperimentPage() {
  const params = useParams();
  const token = Array.isArray(params.token) ? params.token[0] : params.token;

  const { data: experimentInfo, error: experimentError } =
    useSWR<PublicExperimentInfo>(
      token ? `${PUBLIC_API_URL}/experiments/${token}` : null,
      fetcher
    );

  const { data, error, isLoading } = useSWR<Task[]>(
    token ? `${PUBLIC_API_URL}/experiments/${token}/tasks?limit=200` : null,
    fetcher,
    { refreshInterval: 30000 }
  );

  const tasksForExperiment = useMemo(
    () => preparePublicExperimentTasks(data),
    [data]
  );

  const experimentName = experimentInfo?.name || "Public Experiment";
  const hasErrors = Boolean(experimentError || error);
  const scopedApiBaseUrl = token
    ? `${PUBLIC_API_URL}/experiments/${encodeURIComponent(token)}`
    : PUBLIC_API_URL;
  const experimentHref = token
    ? `/share/${encodeURIComponent(token)}`
    : "/share";
  const qaHref =
    token && experimentInfo?.qa_token
      ? buildPublicQaHref(token, experimentInfo.qa_token)
      : null;

  return (
    <>
      <ShareNav />

      <main className="mx-auto w-full max-w-(--breakpoint-2xl) px-4 py-4">
        <div className="space-y-4">
          <ExperimentSectionTabs
            active="experiment"
            experimentHref={experimentHref}
            qaHref={qaHref}
            publicView
          />
          <ExperimentDetailView
            tasksForExperiment={tasksForExperiment}
            isLoading={isLoading}
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
