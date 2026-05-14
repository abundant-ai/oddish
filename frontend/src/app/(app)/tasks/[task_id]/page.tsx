import { auth } from "@clerk/nextjs/server";
import {
  getAuthHeaders,
  getBackendUrl,
  getClerkToken,
} from "@/lib/backend-config";
import type { TaskDetailResponse } from "@/lib/types";
import { TaskDetailClient } from "./task-detail-client";

async function getInitialTaskDetail(
  taskId: string,
): Promise<TaskDetailResponse | null> {
  try {
    const authObj = await auth();
    if (!authObj?.userId) return null;

    const token = await getClerkToken(authObj.getToken);
    if (!token) return null;

    const url = getBackendUrl("tasks", `/${taskId}/detail`);
    const response = await fetch(url, {
      cache: "no-store",
      headers: getAuthHeaders(token),
    });
    if (!response.ok) {
      console.error(
        `[tasks/[task_id]/page] Failed initial task detail fetch: ${response.status}`,
      );
      return null;
    }

    return (await response.json()) as TaskDetailResponse;
  } catch (error) {
    console.error("[tasks/[task_id]/page] Initial task detail fetch failed", error);
    return null;
  }
}

export default async function TaskDetailPage({
  params,
  searchParams,
}: {
  params: Promise<{ task_id: string }>;
  searchParams?: Promise<{ version?: string | string[] }>;
}) {
  const { task_id } = await params;
  const initialDetail = await getInitialTaskDetail(task_id);
  const sp = await searchParams;
  const versionParam = sp?.version;
  const initialVersionId = Array.isArray(versionParam)
    ? versionParam[0]
    : versionParam;

  return (
    <TaskDetailClient
      taskId={task_id}
      initialDetail={initialDetail}
      initialVersionId={initialVersionId ?? null}
    />
  );
}
