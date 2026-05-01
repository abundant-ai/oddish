"use client";

import { useMemo } from "react";
import useSWR from "swr";
import { ResizableDrawer } from "@/components/ui/resizable-drawer";
import { TrialDetailPanel } from "@/components/trial-detail-panel";
import { Skeleton } from "@/components/ui/skeleton";
import { fetcher } from "@/lib/api";
import type { Task, Trial } from "@/lib/types";

interface Props {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  taskId: string;
  trialId: string;
}

export function TrialInspectDrawer({
  open,
  onOpenChange,
  taskId,
  trialId,
}: Props) {
  const url = open && taskId ? `/api/tasks/${encodeURIComponent(taskId)}` : null;
  const { data: task, error } = useSWR<Task>(url, fetcher, {
    refreshInterval: 0,
    revalidateOnFocus: false,
  });

  const trial: Trial | null = useMemo(() => {
    const trials = (task?.trials as Trial[] | undefined) ?? [];
    return trials.find((t) => t.id === trialId) ?? null;
  }, [task, trialId]);

  return (
    <ResizableDrawer
      open={open}
      onOpenChange={onOpenChange}
      defaultWidth={760}
      minWidth={520}
      maxWidth={1400}
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
        <div className="p-4 text-sm text-muted-foreground">
          Trial {trialId} not found on task {taskId}.
        </div>
      ) : (
        <TrialDetailPanel
          isOpen={true}
          onClose={() => onOpenChange(false)}
          trial={trial}
          task={task}
          orderedTrials={(task.trials as Trial[] | undefined) ?? null}
          trialIndex={
            (task.trials as Trial[] | undefined)?.findIndex(
              (t) => t.id === trialId,
            ) ?? null
          }
          allowRetry
          contentOnly
        />
      )}
    </ResizableDrawer>
  );
}
