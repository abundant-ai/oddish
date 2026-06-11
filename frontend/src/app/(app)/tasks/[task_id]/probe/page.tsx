import { ProbeSubmitForm } from "@/components/probe-submit-form";
import { ProbeHistoryTable } from "@/components/probe-history-table";

export default async function ProbePage({
  params,
  searchParams,
}: {
  params: Promise<{ task_id: string }>;
  searchParams: Promise<{ scope?: string; target_trial?: string }>;
}) {
  const { task_id } = await params;
  const { scope, target_trial } = await searchParams;
  return (
    <div className="container mx-auto max-w-3xl py-8 space-y-8">
      <div>
        <h1 className="text-2xl font-semibold">Probe run</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Submit a custom prompt prepended to this task&apos;s instruction.
          The agent runs in local Docker via Harbor.
        </p>
        <p className="mt-1 text-xs text-muted-foreground">
          Task: <span className="font-mono">{task_id}</span>
        </p>
      </div>
      <ProbeSubmitForm
        taskId={task_id}
        initialScope={scope === "trial" ? "trial" : "task"}
        initialTargetTrialId={target_trial ?? null}
      />
      <ProbeHistoryTable taskId={task_id} />
    </div>
  );
}
