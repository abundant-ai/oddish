"use client";

import { useMemo } from "react";
import useSWR from "swr";
import { useParams } from "next/navigation";
import { DatasetDetailView } from "@/components/dataset-detail-view";
import { Nav } from "@/components/nav";
import type { Task } from "@/lib/types";
import { fetcher } from "@/lib/api";

interface PublicExperimentInfo {
  name: string;
  public_token: string;
}

const PUBLIC_API_URL = "/api/public";

export default function PublicDatasetPage() {
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
    { refreshInterval: 30000, revalidateOnFocus: false },
  );

  const tasks = useMemo(() => {
    const taskList = Array.isArray(data) ? [...data] : [];
    return taskList.sort(
      (a, b) =>
        new Date(b.created_at).getTime() - new Date(a.created_at).getTime(),
    );
  }, [data]);

  const datasetName = experimentInfo?.name || "Public Dataset";
  const hasError = Boolean(experimentError || error);

  return (
    <>
      <Nav />

      <main className="px-4 py-4 max-w-screen-2xl mx-auto w-full">
        <DatasetDetailView
          datasetName={datasetName}
          tasks={tasks}
          isLoading={isLoading}
          hasError={hasError}
        />
      </main>
    </>
  );
}
