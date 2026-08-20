import { expandVersionParam } from "@/lib/version-url";
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
  // Expand here rather than in the client: the initial selection has to be a
  // real row id, or the first render filters trials against a bare number.
  const initialVersionId = expandVersionParam(
    Array.isArray(versionParam) ? versionParam[0] : versionParam,
    task_id
  );

  return (
    <TaskDetailClient taskId={task_id} initialVersionId={initialVersionId} />
  );
}
