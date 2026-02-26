"use client";

import { useEffect, useMemo, useState } from "react";
import useSWRInfinite from "swr/infinite";
import { useAuth } from "@clerk/nextjs";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { ExperimentShareButton } from "@/components/experiment-share-button";
import { ExperimentDetailView } from "@/components/experiment-detail-view";
import type { Task } from "@/lib/types";
import { fetcher } from "@/lib/api";
import { Beaker, Pencil } from "lucide-react";
import { encodeExperimentRouteParam } from "@/lib/utils";

const TASKS_PAGE_SIZE = 10;
const ACTIVE_TASK_STATUSES = new Set([
  "pending",
  "queued",
  "running",
  "analyzing",
  "verdict_pending",
]);

type ExperimentClientPageProps = {
  experimentId: string;
  initialTasksPage?: Task[] | null;
};

export function ExperimentClientPage({
  experimentId,
  initialTasksPage = null,
}: ExperimentClientPageProps) {
  const { orgRole } = useAuth();

  const [isEditingName, setIsEditingName] = useState(false);
  const [nameDraft, setNameDraft] = useState("");
  const [nameError, setNameError] = useState<string | null>(null);
  const [isSavingName, setIsSavingName] = useState(false);

  const { data, error, isLoading, isValidating, size, setSize, mutate } =
    useSWRInfinite<Task[]>(
      (pageIndex, previousPageData) => {
        if (!experimentId) return null;
        if (previousPageData && previousPageData.length < TASKS_PAGE_SIZE) {
          return null;
        }
        const offset = pageIndex * TASKS_PAGE_SIZE;
        return `/api/experiments/${encodeExperimentRouteParam(
          experimentId,
        )}/tasks?limit=${TASKS_PAGE_SIZE}&offset=${offset}&include_trials=true`;
      },
      fetcher,
      {
        refreshInterval: (latestData) => {
          const pages = Array.isArray(latestData) ? latestData : [];
          const tasks = pages.flat();
          if (tasks.length === 0) return 5000;
          const hasActiveTasks = tasks.some((task) => {
            const activeTrials = Math.max(
              0,
              task.total - task.completed - task.failed,
            );
            return activeTrials > 0 || ACTIVE_TASK_STATUSES.has(task.status);
          });
          return hasActiveTasks ? 30000 : 90000;
        },
        revalidateOnFocus: false,
        revalidateFirstPage: true,
        persistSize: true,
        fallbackData: initialTasksPage ? [initialTasksPage] : undefined,
      },
    );

  const tasksForExperiment = useMemo(() => {
    const pages = Array.isArray(data) ? data : [];
    const deduped = new Map<string, Task>();
    for (const page of pages) {
      for (const task of page ?? []) {
        deduped.set(task.id, task);
      }
    }
    const taskList = Array.from(deduped.values());
    return taskList.sort(
      (a, b) =>
        new Date(b.created_at).getTime() - new Date(a.created_at).getTime(),
    );
  }, [data]);
  const lastPage = data?.[data.length - 1] ?? null;
  const hasMore = Boolean(lastPage && lastPage.length === TASKS_PAGE_SIZE);
  const isLoadingMore = isValidating && !isLoading;

  const experimentName = tasksForExperiment[0]?.experiment_name ?? "";
  const displayName = experimentName || experimentId || "Experiment";
  const initialName = experimentName || experimentId || "";
  const canManageExperimentShare =
    orgRole === "org:admin" || orgRole === "org:owner";

  useEffect(() => {
    if (!isEditingName) {
      setNameDraft(initialName);
      setNameError(null);
    }
  }, [initialName, isEditingName]);

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
      await mutate();
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

    await mutate();
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
          tasksForExperiment={tasksForExperiment}
          isLoading={isLoading}
          hasError={Boolean(error)}
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
              {hasMore && !isLoading && experimentId && (
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  className="h-7 px-2 text-[10px] uppercase tracking-wide"
                  onClick={() => setSize(size + 1)}
                  disabled={isLoadingMore}
                >
                  {isLoadingMore ? "Loading..." : "Load more"}
                </Button>
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
          onRerun={() => mutate()}
        />
      )}
    </div>
  );
}

export default ExperimentClientPage;
