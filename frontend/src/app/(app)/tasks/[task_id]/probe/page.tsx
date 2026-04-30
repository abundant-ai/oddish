import { ProbeSubmitForm } from "@/components/probe-submit-form";
import { ProbeHistoryTable } from "@/components/probe-history-table";
import { ProbeTaskHeader } from "@/components/probe-task-header";
import { ProbeChatButton } from "@/components/probe-chat-button";

export default async function ProbePage({
  params,
}: {
  params: Promise<{ task_id: string }>;
}) {
  const { task_id } = await params;
  return (
    <div className="container mx-auto max-w-3xl py-8 space-y-8">
      <div className="flex items-start justify-between gap-4">
        <ProbeTaskHeader taskId={task_id} />
        <ProbeChatButton taskId={task_id} />
      </div>
      <ProbeSubmitForm taskId={task_id} />
      <ProbeHistoryTable taskId={task_id} />
    </div>
  );
}
