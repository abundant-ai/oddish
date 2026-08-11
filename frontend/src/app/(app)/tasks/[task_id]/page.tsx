import { TaskDetailClient } from "./task-detail-client";

export default async function TaskDetailPage({
  params,
  searchParams,
}: {
  params: Promise<{ task_id: string }>;
  searchParams?: Promise<{ version?: string | string[] }>;
}) {
  const { task_id } = await params;
  const sp = await searchParams;
  const versionParam = sp?.version;
  const initialVersionId = Array.isArray(versionParam)
    ? versionParam[0]
    : versionParam;

  return (
    <TaskDetailClient
      taskId={task_id}
      initialVersionId={initialVersionId ?? null}
    />
  );
}
