import { auth } from "@clerk/nextjs/server";
import {
  getAuthHeaders,
  getBackendUrl,
  getClerkToken,
} from "@/lib/backend-config";
import type { EvidenceCell, Task, TaskVersion } from "@/lib/types";
import { TaskDetailClient } from "./task-detail-client";

async function fetchBackendJson<T>(
  token: string,
  endpoint: string,
  path = "",
  queryParams?: Record<string, string>
): Promise<T | null> {
  const response = await fetch(getBackendUrl(endpoint, path, queryParams), {
    cache: "no-store",
    headers: getAuthHeaders(token),
  });
  if (!response.ok) {
    console.error(
      `[task/detail] Backend fetch failed ${endpoint}${path}: ${response.status}`
    );
    return null;
  }
  return (await response.json()) as T;
}

export default async function TaskDetailPage({
  params,
  searchParams,
}: {
  params: Promise<{ task: string }>;
  searchParams?: Promise<{ version?: string | string[] }>;
}) {
  const authObj = await auth();
  const token = authObj?.userId ? await getClerkToken(authObj.getToken) : null;
  const { task: taskId } = await params;
  const query = await searchParams;
  const requestedVersionParam = Array.isArray(query?.version)
    ? query?.version[0]
    : query?.version;

  if (!token) {
    return (
      <TaskDetailClient
        taskId={taskId}
        task={null}
        versions={[]}
        selectedVersion={null}
        initialEvidence={[]}
      />
    );
  }

  const [task, versions] = await Promise.all([
    fetchBackendJson<Task>(token, "tasks", `/${encodeURIComponent(taskId)}`, {
      include_trials: "false",
    }),
    fetchBackendJson<TaskVersion[]>(
      token,
      "tasks",
      `/${encodeURIComponent(taskId)}/versions`
    ),
  ]);

  const requestedVersion = requestedVersionParam
    ? Number(requestedVersionParam)
    : null;
  const selectedVersion =
    Number.isFinite(requestedVersion) && requestedVersion
      ? requestedVersion
      : (task?.current_version ?? versions?.[0]?.version ?? null);

  const initialEvidence =
    selectedVersion == null
      ? []
      : ((await fetchBackendJson<EvidenceCell[]>(
          token,
          "tasks",
          `/${encodeURIComponent(taskId)}/versions/${selectedVersion}/evidence`
        )) ?? []);

  return (
    <TaskDetailClient
      taskId={taskId}
      task={task}
      versions={versions ?? []}
      selectedVersion={selectedVersion}
      initialEvidence={initialEvidence}
    />
  );
}
