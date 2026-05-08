import type { Metadata } from "next";
import { TaskDetailClient } from "./task-detail-client";

type Params = Promise<{ task_id: string }>;

export async function generateMetadata({
  params,
}: {
  params: Params;
}): Promise<Metadata> {
  const { task_id } = await params;
  return {
    title: `Task ${task_id} · Oddish`,
    description: "Task versions and evidence accumulated against each.",
  };
}

export default async function TaskDetailPage({ params }: { params: Params }) {
  const { task_id } = await params;
  return <TaskDetailClient taskId={task_id} />;
}
