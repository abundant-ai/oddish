"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import useSWR from "swr";
import useSWRInfinite from "swr/infinite";
import { useSWRConfig } from "swr";
import { useAuth } from "@clerk/nextjs";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { ExperimentShareButton } from "@/components/experiment-share-button";
import { ExperimentDetailView } from "@/components/experiment-detail-view";
import type { Task } from "@/lib/types";
import { fetcher } from "@/lib/api";
import { Beaker, Loader2, Pencil } from "lucide-react";
import { encodeExperimentRouteParam } from "@/lib/utils";

const TRIALS_BATCH_SIZE = 50;
const ACTIVE_TASK_STATUSES = new Set([
  "pending",
  "queued",
  "running",
  "analyzing",
  "verdict_pending",
]);

type ExperimentClientPageProps = {
  experimentId: string;
};

export function ExperimentClientPage({
  experimentId,
}: ExperimentClientPageProps) {
  const { orgRole } = useAuth();
  const { mutate: mutateKey } = useSWRConfig();

  const [isEditingName, setIsEditingName] = useState(false);
  const [nameDraft, setNameDraft] = useState("");
  const [nameError, setNameError] = useState<string | null>(null);
  const [isSavingName, setIsSavingName] = useState(false);

  const encodedId = experimentId
    ? encodeExperimentRouteParam(experimentId)
    : "";

  // Phase 1: Fetch ALL tasks without trial data (lightweight).
  // Populates the full task list immediately.
  const allTasksUrl = experimentId
    ? `/api/experiments/${encodedId}/tasks?limit=2000&offset=0&include_trials=false`
    : null;

  const {
    data: lightweightTasks,
    error: lightweightError,
    isLoading: isLoadingTasks,
    mutate: mutateLightweight,
  } = useSWR<Task[]>(allTasksUrl, fetcher, {
    refreshInterval: 0,
    revalidateOnFocus: false,
  });

  // Phase 2: Progressively fetch compact trial data in batches.
  const getTrialsPageKey = useCallback(
    (pageIndex: number, previousPageData: Task[] | null) => {
      if (!experimentId || !encodedId) return null;
      if (previousPageData && previousPageData.length < TRIALS_BATCH_SIZE)
        return null;
      const offset = pageIndex * TRIALS_BATCH_SIZE;
      return `/api/experiments/${encodedId}/tasks?limit=${TRIALS_BATCH_SIZE}&offset=${offset}&include_trials=true`;
    },
    [experimentId, encodedId],
  );

  const {
    data: trialPages,
    isLoading: isLoadingTrialPages,
    isValidating: isValidatingTrials,
    size: trialsSize,
    setSize: setTrialsSize,
    mutate: mutateTrials,
  } = useSWRInfinite<Task[]>(getTrialsPageKey, fetcher, {
    refreshInterval: 0,
    revalidateOnFocus: false,
    revalidateFirstPage: false,
    persistSize: true,
  });

  // Auto-advance: once a batch arrives and there's more data, request next
  const trialsLastPage = trialPages?.[trialPages.length - 1] ?? null;
  const hasMoreTrials = Boolean(
    trialsLastPage && trialsLastPage.length === TRIALS_BATCH_SIZE,
  );
  const allTrialPagesLoaded = trialPages
    ? trialPages.length === trialsSize
    : false;

  useEffect(() => {
    if (
      allTrialPagesLoaded &&
      !isValidatingTrials &&
      hasMoreTrials
    ) {
      const timeout = setTimeout(() => {
        setTrialsSize((s) => s + 1);
      }, 50);
      return () => clearTimeout(timeout);
    }
  }, [allTrialPagesLoaded, isValidatingTrials, hasMoreTrials, setTrialsSize]);

  // Merge lightweight task shells with trial-enriched data
  const tasksForExperiment = useMemo(() => {
    const trialDataById = new Map<string, Task>();
    for (const page of trialPages ?? []) {
      for (const task of page ?? []) {
        trialDataById.set(task.id, task);
      }
    }

    const base = lightweightTasks ?? [];
    const seenIds = new Set<string>();
    const merged: Task[] = [];

    for (const task of base) {
      seenIds.add(task.id);
      merged.push(trialDataById.get(task.id) ?? task);
    }

    for (const [id, task] of trialDataById) {
      if (!seenIds.has(id)) {
        merged.push(task);
      }
    }

    return merged.sort(
      (a, b) =>
        new Date(b.created_at).getTime() - new Date(a.created_at).getTime(),
    );
  }, [lightweightTasks, trialPages]);

  const isLoading = isLoadingTasks;
  const isLoadingTrials =
    (lightweightTasks?.length ?? 0) > 0 &&
    (isLoadingTrialPages || isValidatingTrials || hasMoreTrials);
  const trialsLoadedCount = useMemo(() => {
    if (!trialPages) return 0;
    return trialPages.reduce((sum, page) => sum + (page?.length ?? 0), 0);
  }, [trialPages]);

  const refreshIntervalMs = useMemo(() => {
    if (tasksForExperiment.length === 0) return 5000;
    const hasActiveTasks = tasksForExperiment.some((task) => {
      const activeTrials = Math.max(
        0,
        task.total - task.completed - task.failed,
      );
      return activeTrials > 0 || ACTIVE_TASK_STATUSES.has(task.status);
    });
    return hasActiveTasks ? 30000 : 90000;
  }, [tasksForExperiment]);

  const experimentName = tasksForExperiment[0]?.experiment_name ?? "";
  const displayName = experimentName || experimentId || "Experiment";
  const initialName = experimentName || experimentId || "";
  const canManageExperimentShare =
    orgRole === "org:admin" || orgRole === "org:owner";

  const refreshTaskPages = useCallback(
    async (_taskIds?: string[]) => {
      await Promise.all([mutateLightweight(), mutateTrials()]);
    },
    [mutateLightweight, mutateTrials],
  );

  useEffect(() => {
    if (!isEditingName) {
      setNameDraft(initialName);
      setNameError(null);
    }
  }, [initialName, isEditingName]);

  useEffect(() => {
    if (!allTasksUrl) return;

    const intervalId = window.setInterval(() => {
      void mutateLightweight();
      const firstTrialKey = getTrialsPageKey(0, null);
      if (firstTrialKey) void mutateKey(firstTrialKey);
    }, refreshIntervalMs);

    return () => {
      window.clearInterval(intervalId);
    };
  }, [
    allTasksUrl,
    refreshIntervalMs,
    mutateLightweight,
    mutateKey,
    getTrialsPageKey,
  ]);

  const handleRename = async () => {
    if (!experimentId) return;
    const nextName = nameDraft.trim();
    if (!nextName) {
      setNameError("Experiment name cannot be empty.");
      return;
    }

    setIsSavingName(true);
    setNameError(null);

    try {
      const res = await fetch(
        `/api/experiments/${encodeExperimentRouteParam(experimentId)}`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: nextName }),
        },
      );

      if (!res.ok) {
        const errorData = await res.json().catch(() => ({}));
        throw new Error(
          errorData.detail || errorData.error || "Failed to rename experiment",
        );
      }

      setIsEditingName(false);
      await mutateLightweight(
        (tasks) =>
          tasks?.map((task) => ({ ...task, experiment_name: nextName })),
        { revalidate: false },
      );
      await mutateTrials(
        (pages) =>
          pages?.map((page) =>
            page?.map((task) => ({ ...task, experiment_name: nextName })),
          ),
        { revalidate: false },
      );
      void refreshTaskPages();
    } catch (err) {
      setNameError(err instanceof Error ? err.message : "Rename failed");
    } finally {
      setIsSavingName(false);
    }
  };

  const handleDeleteTask = async (task: Task) => {
    const res = await fetch(`/api/tasks/${encodeURIComponent(task.id)}`, {
      method: "DELETE",
    });

    if (!res.ok) {
      const errorData = await res.json().catch(() => ({}));
      throw new Error(
        errorData.detail || errorData.error || "Failed to delete task",
      );
    }

    await mutateLightweight(
      (tasks) => tasks?.filter((item) => item.id !== task.id),
      { revalidate: false },
    );
    await mutateTrials(
      (pages) =>
        pages?.map((page) => page?.filter((item) => item.id !== task.id)),
      { revalidate: false },
    );
    await refreshTaskPages();
  };

  return (
    <div className="space-y-4">
      {!experimentId ? (
        <Alert>
          <AlertTitle>Missing experiment</AlertTitle>
          <AlertDescription>
            Select an experiment from the dashboard.
          </AlertDescription>
        </Alert>
      ) : (
        <ExperimentDetailView
          experimentId={experimentId}
          tasksForExperiment={tasksForExperiment}
          isLoading={isLoading}
          isLoadingTrials={isLoadingTrials}
          hasError={Boolean(lightweightError)}
          headerLeft={
            <div className="flex items-center gap-2">
              <Beaker className="h-4 w-4 text-muted-foreground" />
              {isEditingName ? (
                <div className="flex items-center gap-2">
                  <Input
                    value={nameDraft}
                    onChange={(event) => setNameDraft(event.target.value)}
                    className="h-8 w-[220px]"
                    placeholder="Experiment name"
                  />
                  <Button
                    type="button"
                    size="sm"
                    className="h-8"
                    onClick={handleRename}
                    disabled={isSavingName}
                  >
                    {isSavingName ? "Saving..." : "Save"}
                  </Button>
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    className="h-8"
                    onClick={() => setIsEditingName(false)}
                    disabled={isSavingName}
                  >
                    Cancel
                  </Button>
                </div>
              ) : (
                <div className="flex items-center gap-2">
                  <div className="text-sm font-medium">{displayName}</div>
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon"
                    className="h-7 w-7"
                    onClick={() => setIsEditingName(true)}
                    disabled={!experimentId}
                    aria-label="Rename experiment"
                    title="Rename experiment"
                  >
                    <Pencil className="h-4 w-4" />
                  </Button>
                </div>
              )}
            </div>
          }
          headerRight={
            <>
              {isLoadingTrials && (
                <div className="flex items-center gap-1.5 text-[10px] text-muted-foreground">
                  <Loader2 className="h-3 w-3 animate-spin" />
                  <span>
                    Loading trials
                    {lightweightTasks
                      ? ` ${trialsLoadedCount}/${lightweightTasks.length}`
                      : ""}
                    …
                  </span>
                </div>
              )}
              {experimentId && (
                <ExperimentShareButton
                  experimentId={experimentId}
                  canManageShare={canManageExperimentShare}
                />
              )}
            </>
          }
          inlineAlert={
            nameError ? (
              <Alert variant="destructive">
                <AlertTitle>Rename failed</AlertTitle>
                <AlertDescription>{nameError}</AlertDescription>
              </Alert>
            ) : null
          }
          readOnly={false}
          allowRetry
          onTaskDelete={handleDeleteTask}
          onRerun={refreshTaskPages}
        />
      )}
    </div>
  );
}
