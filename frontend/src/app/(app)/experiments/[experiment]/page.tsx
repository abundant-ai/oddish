import { auth } from "@clerk/nextjs/server";
import {
  getAuthHeaders,
  getBackendUrl,
  getClerkToken,
} from "@/lib/backend-config";
import { decodeExperimentRouteParam } from "@/lib/utils";
import { ExperimentClientPage } from "./experiment-client";
import type { Task } from "@/lib/types";

async function getInitialTasks(
  experimentId: string,
): Promise<Task[] | null> {
  try {
    const authObj = await auth();
    if (!authObj?.userId) return null;

    const token = await getClerkToken(authObj.getToken);
    if (!token) return null;

    const url = getBackendUrl("tasks", "", {
      experiment_id: experimentId,
      limit: "2000",
      offset: "0",
      include_trials: "false",
    });
    const response = await fetch(url, {
      cache: "no-store",
      headers: getAuthHeaders(token),
    });
    if (!response.ok) {
      console.error(
        `[experiment/page] Failed initial tasks fetch: ${response.status}`,
      );
      return null;
    }
    return (await response.json()) as Task[];
  } catch (error) {
    console.error("[experiment/page] Initial tasks fetch failed", error);
    return null;
  }
}

export default async function ExperimentDetailPage({
  params,
}: {
  params: Promise<{ experiment: string }>;
}) {
  const { experiment } = await params;
  const experimentId = decodeExperimentRouteParam(experiment ?? "");
  const initialTasks = await getInitialTasks(experimentId);

  return (
    <ExperimentClientPage
      experimentId={experimentId}
      initialTasks={initialTasks}
    />
  );
}
