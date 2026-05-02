"use client";

import { useMemo } from "react";
import useSWR from "swr";
import { ResizableDrawer } from "@/components/ui/resizable-drawer";
import { Skeleton } from "@/components/ui/skeleton";
import { TrialDetailPanel } from "@/components/trial-detail-panel";
import { fetcher } from "@/lib/api";
import type { Task, Trial } from "@/lib/types";

type TrialInspectDrawerProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  taskId: string;
  trialId: string;
  siblingTrialIds?: string[];
  onTrialChange?: (trialId: string) => void;
  noBackdrop?: boolean;
};

export function TrialInspectDrawer({
  open,
  onOpenChange,
  taskId,
  trialId,
  siblingTrialIds,
  onTrialChange,
  noBackdrop = false,
}: TrialInspectDrawerProps) {
  const url = open
    ? `/api/tasks/${encodeURIComponent(taskId)}?include_trials=true`
    : null;
  const { data: task, error } = useSWR<Task>(url, fetcher, {
    revalidateOnFocus: false,
  });

  const allTrials = useMemo<Trial[]>(
    () => (task?.trials as Trial[] | undefined) ?? [],
    [task]
  );
  const trial = useMemo(
    () => allTrials.find((candidate) => candidate.id === trialId) ?? null,
    [allTrials, trialId]
  );
  const orderedTrials = useMemo(() => {
    if (!siblingTrialIds?.length) return allTrials;
    const byId = new Map(
      allTrials.map((candidate) => [candidate.id, candidate])
    );
    return siblingTrialIds
      .map((id) => byId.get(id))
      .filter((candidate): candidate is Trial => Boolean(candidate));
  }, [allTrials, siblingTrialIds]);
  const trialIndex = orderedTrials.findIndex(
    (candidate) => candidate.id === trialId
  );

  return (
    <ResizableDrawer
      open={open}
      onOpenChange={onOpenChange}
      defaultWidth={760}
      minWidth={520}
      maxWidth={1400}
      noBackdrop={noBackdrop}
    >
      {error ? (
        <div className="p-4 text-sm">
          Failed to load trial: {String((error as Error).message)}
        </div>
      ) : !task ? (
        <div className="p-4">
          <Skeleton className="h-64 w-full" />
        </div>
      ) : !trial ? (
        <div className="text-muted-foreground p-4 text-sm">
          Trial {trialId} was not found on task {taskId}.
        </div>
      ) : (
        <TrialDetailPanel
          isOpen
          onClose={() => onOpenChange(false)}
          trial={trial}
          task={task}
          orderedTrials={orderedTrials}
          trialIndex={trialIndex >= 0 ? trialIndex : null}
          onNavigate={(nextTrial) => onTrialChange?.(nextTrial.id)}
          allowRetry
          contentOnly
        />
      )}
    </ResizableDrawer>
  );
}
