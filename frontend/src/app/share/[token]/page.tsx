"use client";

import { useMemo } from "react";
import { useParams } from "next/navigation";
import useSWR from "swr";
import { Beaker } from "lucide-react";
import { ExperimentDetailView } from "@/components/experiment-detail-view";
import { Nav } from "@/components/nav";
import type { Task } from "@/lib/types";
import { fetcher } from "@/lib/api";

interface PublicExperimentInfo {
  name: string;
  public_token: string;
}

const PUBLIC_API_URL = `/api/public`;

export default function PublicExperimentPage() {
  const params = useParams();
  const token = Array.isArray(params.token) ? params.token[0] : params.token;

  const { data: experimentInfo, error: experimentError } =
    useSWR<PublicExperimentInfo>(
      token ? `${PUBLIC_API_URL}/experiments/${token}` : null,
      fetcher,
    );

  const { data, error, isLoading } = useSWR<Task[]>(
    token ? `${PUBLIC_API_URL}/experiments/${token}/tasks?limit=200` : null,
    fetcher,
    { refreshInterval: 30000 },
  );

  const tasksForExperiment = useMemo(() => {
    const taskList = Array.isArray(data) ? [...data] : [];
    return taskList.sort(
      (a, b) =>
        new Date(b.created_at).getTime() - new Date(a.created_at).getTime(),
    );
  }, [data]);

  const experimentName = experimentInfo?.name || "Public Experiment";
  const hasErrors = Boolean(experimentError || error);

  return (
    <>
      <Nav />

      <main className="px-4 py-4 max-w-screen-2xl mx-auto w-full">
        <div className="space-y-4">
          <ExperimentDetailView
            tasksForExperiment={tasksForExperiment}
            isLoading={isLoading}
            hasError={hasErrors}
            errorTitle="Failed to load experiment"
            errorDescription="The share link may be invalid or no longer public."
            headerLeft={
              <div className="flex items-center gap-2">
                <Beaker className="h-4 w-4 text-muted-foreground" />
                <div className="text-sm font-medium">{experimentName}</div>
              </div>
            }
            readOnly
            allowRetry={false}
            apiBaseUrl={PUBLIC_API_URL}
          />
        </div>
      </main>
    </>
  );
}
