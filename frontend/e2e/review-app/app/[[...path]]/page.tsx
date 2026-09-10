import { TaskDetailClient } from "@/app/(app)/tasks/[task_id]/task-detail-client";
import { DeliveryBoardClient } from "@/app/(app)/deliveries/[delivery]/delivery-board-client";
import { expandVersionParam } from "@/lib/version-url";
import { FixtureExperiment } from "../../experiment";
export default async function Page({
  params,
  searchParams,
}: {
  params: Promise<{ path?: string[] }>;
  searchParams: Promise<{ version?: string }>;
}) {
  const { path = [] } = await params;
  const query = await searchParams;
  if (path[0] === "tasks")
    return (
      <TaskDetailClient
        key={path[1]}
        taskId={path[1]}
        initialVersionId={expandVersionParam(query.version, path[1])}
      />
    );
  if (path[0] === "experiments") return <FixtureExperiment />;
  return <DeliveryBoardClient deliveryId="review-demo" initialBoard={null} />;
}
