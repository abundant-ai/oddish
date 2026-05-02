import { TaskDetailClient } from "./task-detail-client";

export default async function TaskDetailPage({
  params,
}: {
  params: Promise<{ task: string }>;
}) {
  const { task } = await params;
  return <TaskDetailClient taskId={task} />;
}
