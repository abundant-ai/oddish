import { FreeformSubmitForm } from "@/components/freeform-submit-form";
import { FreeformHistoryTable } from "@/components/freeform-history-table";

export default async function FreeformAgentPage({
  params,
}: {
  params: Promise<{ task_id: string }>;
}) {
  const { task_id } = await params;
  return (
    <div className="container mx-auto max-w-3xl py-8 space-y-8">
      <div>
        <h1 className="text-2xl font-semibold">Freeform run</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Submit a custom prompt prepended to this task&apos;s instruction. The
          agent runs in local Docker via Harbor.
        </p>
        <p className="mt-1 text-xs text-muted-foreground">
          Task: <span className="font-mono">{task_id}</span>
        </p>
      </div>
      <FreeformSubmitForm taskId={task_id} />
      <FreeformHistoryTable taskId={task_id} />
    </div>
  );
}
