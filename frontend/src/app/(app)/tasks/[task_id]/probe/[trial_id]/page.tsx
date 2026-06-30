"use client";

import { use } from "react";
import Link from "next/link";
import { ProbeDetailPanel } from "@/components/probe-detail-panel";

export default function ProbeResultPage({
  params,
}: {
  params: Promise<{ task_id: string; trial_id: string }>;
}) {
  const { task_id, trial_id } = use(params);

  return (
    <div className="container mx-auto max-w-4xl py-8 space-y-6">
      <Link
        href={`/tasks/${task_id}/probe`}
        className="text-xs underline text-muted-foreground hover:text-foreground"
      >
        ← Back to workbench
      </Link>
      <ProbeDetailPanel
        taskId={task_id}
        trialId={trial_id}
        isOpen
        onClose={() => {}}
        contentOnly
      />
    </div>
  );
}
