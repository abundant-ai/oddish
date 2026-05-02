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
  /**
   * Ordered list of trial IDs to navigate between (prev / next inside
   * the drawer). Defaults to all trials on the parent task. Pass a
   * subset (e.g. trials in a specific cell or version) to scope
   * navigation to that context.
   */
  siblingTrialIds?: string[];
  /**
   * Called when the user picks a different trial via the in-panel
   * navigation. Parent updates its inspecting state with the new id.
   */
  onTrialChange?: (trialId: string) => void;
  /**
   * Suppress the backdrop so the page below the drawer stays
   * interactive (e.g. files panel on the task detail page).
   */
  noBackdrop?: boolean;
}

export function TrialInspectDrawer({
  open,
  onOpenChange,
  taskId,
  trialId,
  siblingTrialIds,
  onTrialChange,
  noBackdrop = false,
}: Props) {
  const url = open && taskId ? `/api/tasks/${encodeURIComponent(taskId)}` : null;
  const { data: task, error } = useSWR<Task>(url, fetcher, {
    refreshInterval: 0,
    revalidateOnFocus: false,
  });

  const allTrials: Trial[] = useMemo(
    () => (task?.trials as Trial[] | undefined) ?? [],
    [task],
  );

  const trial: Trial | null = useMemo(
    () => allTrials.find((t) => t.id === trialId) ?? null,
    [allTrials, trialId],
  );

  // Order trials for prev/next navigation.
  const orderedTrials: Trial[] = useMemo(() => {
    if (siblingTrialIds && siblingTrialIds.length > 0) {
      const byId = new Map(allTrials.map((t) => [t.id, t]));
      return siblingTrialIds
        .map((id) => byId.get(id))
        .filter((t): t is Trial => Boolean(t));
    }
    return allTrials;
  }, [allTrials, siblingTrialIds]);

  const trialIndex = useMemo(
    () => orderedTrials.findIndex((t) => t.id === trialId),
    [orderedTrials, trialId],
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
        <div className="p-4 text-sm text-muted-foreground">
          Trial {trialId} not found on task {taskId}.
        </div>
      ) : (
        <TrialDetailPanel
          isOpen={true}
          onClose={() => onOpenChange(false)}
          trial={trial}
          task={task}
          orderedTrials={orderedTrials}
          trialIndex={trialIndex >= 0 ? trialIndex : null}
          onNavigate={(nextTrial) => {
            if (onTrialChange) onTrialChange(nextTrial.id);
          }}
          allowRetry
          contentOnly
        />
      )}
    </ResizableDrawer>
  );
}
