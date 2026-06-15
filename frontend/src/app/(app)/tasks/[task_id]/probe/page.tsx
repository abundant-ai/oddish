import Link from "next/link";
import { ProbeSubmitForm } from "@/components/probe-submit-form";
import { ProbeHistoryTable } from "@/components/probe-history-table";

export default async function ProbePage({
  params,
}: {
  params: Promise<{ task_id: string }>;
}) {
  const { task_id } = await params;
  return (
    <div className="container mx-auto max-w-3xl py-8 space-y-8">
      <div>
        <h1 className="text-2xl font-semibold">Probe run</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Submit a custom prompt prepended to this task&apos;s instruction.
          The agent runs in local Docker via Harbor.
        </p>
        <p className="mt-1 text-sm">
          Task:{" "}
          <Link
            href={`/tasks/${task_id}`}
            className="font-mono font-medium text-blue-600 underline-offset-4 hover:text-blue-700 hover:underline"
          >
            {task_id}
          </Link>
        </p>
      </div>
      <ProbeSubmitForm taskId={task_id} />
      <ProbeHistoryTable taskId={task_id} />
    </div>
  );
}
