"use client";

import { useCallback, useEffect, useMemo, useRef } from "react";
import useSWR from "swr";
import useSWRInfinite from "swr/infinite";
import { fetcher } from "@/lib/api";
import { encodeExperimentRouteParam } from "@/lib/utils";
import type {
  ExperimentAccess,
  ExperimentCostTotals,
  ExperimentOpenResponse,
  ExperimentRevisionResponse,
  ExperimentTrialPageResponse,
  Task,
  Trial,
} from "@/lib/types";

function pageBase(access: ExperimentAccess): string | null {
  if (access.kind === "member") {
    return access.experimentId
      ? `/api/experiments/${encodeExperimentRouteParam(access.experimentId)}`
      : null;
  }
  return access.token
    ? `/api/public/experiments/${encodeURIComponent(access.token)}`
    : null;
}

export function useExperimentPageResources(access: ExperimentAccess) {
  const base = pageBase(access);

  const getOpenKey = useCallback(
    (
      pageIndex: number,
      previousPage: ExperimentOpenResponse | null
    ): string | null => {
      if (!base) return null;
      if (pageIndex === 0) return `${base}/open?limit=100`;
      if (!previousPage?.next_cursor) return null;
      return `${base}/open?limit=100&cursor=${encodeURIComponent(previousPage.next_cursor)}`;
    },
    [base]
  );
  const {
    data: openPages,
    error: openError,
    isLoading: isLoadingOpen,
    isValidating: isValidatingOpen,
    setSize: setOpenSize,
    mutate: mutateOpen,
  } = useSWRInfinite<ExperimentOpenResponse>(getOpenKey, fetcher, {
    revalidateOnFocus: false,
    revalidateFirstPage: false,
  });
  const open = openPages?.[0];
  const revision = open?.revision;

  const getTrialPageKey = useCallback(
    (
      pageIndex: number,
      previousPage: ExperimentTrialPageResponse | null
    ): string | null => {
      if (!base || !revision) return null;
      if (pageIndex > 0 && !previousPage?.next_cursor) return null;
      const cursor =
        pageIndex > 0 && previousPage?.next_cursor
          ? `&cursor=${encodeURIComponent(previousPage.next_cursor)}`
          : "";
      // The revision is part of the SWR identity even though the backend does
      // not need it as a query filter. A changed experiment snapshot therefore
      // gets fresh pages and resets SWR Infinite to page one.
      return `${base}/trial-page?limit=250&revision=${encodeURIComponent(revision)}${cursor}`;
    },
    [base, revision]
  );
  const {
    data: trialPages,
    error: trialError,
    isLoading: isLoadingTrialPages,
    isValidating: isValidatingTrials,
    setSize: setTrialSize,
    mutate: mutateTrialPages,
  } = useSWRInfinite<ExperimentTrialPageResponse>(getTrialPageKey, fetcher, {
    revalidateOnFocus: false,
    revalidateFirstPage: false,
  });

  const costKey =
    access.kind === "member" && base && open ? `${base}/cost-totals` : null;
  const {
    data: costTotals,
    error: costError,
    mutate: mutateCostTotals,
  } = useSWR<ExperimentCostTotals>(costKey, fetcher, {
    revalidateOnFocus: false,
  });

  const revisionKey =
    base && open?.has_active_trials ? `${base}/revision` : null;
  const { data: polledRevision } = useSWR<ExperimentRevisionResponse>(
    revisionKey,
    fetcher,
    { refreshInterval: 5000, revalidateOnFocus: false }
  );
  const appliedPollRef = useRef<string | null>(null);
  useEffect(() => {
    if (!open || !polledRevision) return;
    const pollIdentity = `${polledRevision.revision}:${polledRevision.has_active_trials}`;
    if (appliedPollRef.current === pollIdentity) return;
    const revisionChanged = polledRevision.revision !== open.revision;
    const justSettled =
      open.has_active_trials && !polledRevision.has_active_trials;
    if (!revisionChanged && !justSettled) return;

    appliedPollRef.current = pollIdentity;
    const refreshes: Array<Promise<unknown>> = [mutateOpen()];
    if (!revisionChanged) refreshes.push(mutateTrialPages());
    if (access.kind === "member") refreshes.push(mutateCostTotals());
    void Promise.all(refreshes);
  }, [
    access.kind,
    mutateCostTotals,
    mutateOpen,
    mutateTrialPages,
    open,
    polledRevision,
  ]);

  const taskRows = useMemo(() => {
    const byId = new Map<string, ExperimentOpenResponse["tasks"][number]>();
    for (const page of openPages ?? []) {
      for (const task of page.tasks) byId.set(task.id, task);
    }
    return Array.from(byId.values());
  }, [openPages]);

  const trialCells = useMemo(() => {
    const byId = new Map<
      string,
      ExperimentTrialPageResponse["trials"][number]
    >();
    for (const page of trialPages ?? []) {
      for (const trial of page.trials) byId.set(trial.id, trial);
    }
    return Array.from(byId.values());
  }, [trialPages]);

  const trialsByTask = useMemo(() => {
    const grouped = new Map<string, typeof trialCells>();
    for (const trial of trialCells) {
      const trials = grouped.get(trial.task_id) ?? [];
      trials.push(trial);
      grouped.set(trial.task_id, trials);
    }
    return grouped;
  }, [trialCells]);

  const tasks = useMemo<Task[]>(() => {
    if (!open) return [];
    return taskRows.map((row) => {
      const trials: Trial[] = (trialsByTask.get(row.id) ?? [])
        .filter((trial) => trial.task_version_id === row.trial_version_id)
        .map((trial) => ({
          ...trial,
          task_path: row.task_path,
          experiment_id:
            access.kind === "member" && trial.owned_here
              ? open.experiment_id
              : null,
          analysis_status: trial.analysis.status,
          analysis: trial.analysis.classification
            ? {
                classification: trial.analysis.classification,
                subtype: trial.analysis.subtype ?? "",
              }
            : null,
          analysis_started_at: trial.analysis.started_at,
          analysis_finished_at: trial.analysis.finished_at,
        }));
      return {
        ...row,
        experiment_id: open.experiment_id,
        experiment_name: open.name,
        experiment_is_public: access.kind === "public",
        progress: `${row.completed}/${row.total} completed`,
        verdict: row.verdict ?? null,
        trials,
      } as Task;
    });
  }, [access.kind, open, taskRows, trialsByTask]);

  const lastOpenPage = openPages?.[openPages.length - 1];
  const lastTrialPage = trialPages?.[trialPages.length - 1];
  const hasMoreTasks = Boolean(lastOpenPage?.next_cursor);
  const hasMoreTrials = Boolean(lastTrialPage?.next_cursor);
  const incompleteTaskIds = useMemo(
    () =>
      new Set(
        tasks
          .filter(
            (task) =>
              (task.trials?.length ?? 0) < task.total &&
              (hasMoreTrials || isLoadingTrialPages || isValidatingTrials)
          )
          .map((task) => task.id)
      ),
    [hasMoreTrials, isLoadingTrialPages, isValidatingTrials, tasks]
  );

  const refresh = useCallback(async () => {
    await Promise.all([
      mutateOpen(),
      mutateTrialPages(),
      access.kind === "member" ? mutateCostTotals() : Promise.resolve(),
    ]);
  }, [access.kind, mutateCostTotals, mutateOpen, mutateTrialPages]);

  return {
    open,
    tasks,
    costTotals,
    costTotalsPending: Boolean(costKey && !costTotals && !costError),
    isLoading: isLoadingOpen,
    isLoadingTrials: isLoadingTrialPages || isValidatingTrials,
    isValidatingOpen,
    isValidatingTrials,
    fatalError: openError && !open ? openError : null,
    openError,
    trialError,
    costError,
    hasMoreTasks,
    hasMoreTrials,
    incompleteTaskIds,
    loadedTrialCount: trialCells.length,
    loadMoreTasks: () => setOpenSize((size) => size + 1),
    loadMoreTrials: () => setTrialSize((size) => size + 1),
    retryOpen: () => mutateOpen(),
    retryTrials: () => mutateTrialPages(),
    refresh,
  };
}
