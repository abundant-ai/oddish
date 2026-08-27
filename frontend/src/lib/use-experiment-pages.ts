"use client";

import { useCallback, useEffect, useMemo } from "react";
import useSWRInfinite from "swr/infinite";
import { fetcher } from "@/lib/api";
import type {
  ExperimentOpenResponse,
  ExperimentTrialCell,
  ExperimentTrialPageResponse,
  Task,
  Trial,
} from "@/lib/types";

const OPEN_PAGE_SIZE = 100;
const TRIAL_PAGE_SIZE = 250;

function trialFromCell(cell: ExperimentTrialCell): Trial {
  const { analysis, ...trial } = cell;
  return {
    ...trial,
    analysis_status: analysis.status,
    analysis_started_at: analysis.started_at,
    analysis_finished_at: analysis.finished_at,
    analysis:
      analysis.classification && analysis.subtype
        ? {
            classification: analysis.classification,
            subtype: analysis.subtype,
            evidence: analysis.evidence ?? undefined,
          }
        : null,
  };
}

function buildTasks(
  openPages: ExperimentOpenResponse[] | undefined,
  trialPages: ExperimentTrialPageResponse[] | undefined,
  publicView: boolean
): Task[] {
  const experiment = openPages?.[0];
  if (!experiment) return [];
  const trialsByTask = new Map<string, Trial[]>();
  for (const page of trialPages ?? []) {
    for (const cell of page.trials) {
      const trials = trialsByTask.get(cell.task_id) ?? [];
      trials.push(trialFromCell(cell));
      trialsByTask.set(cell.task_id, trials);
    }
  }
  return openPages.flatMap((page) =>
    page.tasks.map((task) => ({
      ...task,
      experiment_id: experiment.experiment_id,
      experiment_name: experiment.name,
      experiment_is_public: publicView,
      experiment_created_at: experiment.created_at,
      experiment_owner: experiment.owner,
      experiment_link: experiment.link,
      trials: trialsByTask.get(task.id),
    }))
  );
}

export function useExperimentPages({
  openUrl,
  trialPageUrl,
  publicView = false,
}: {
  openUrl: string | null;
  trialPageUrl: string | null;
  publicView?: boolean;
}) {
  const getOpenKey = useCallback(
    (pageIndex: number, previous: ExperimentOpenResponse | null) => {
      if (!openUrl || (pageIndex > 0 && !previous?.next_task_id)) return null;
      const query = new URLSearchParams({ limit: String(OPEN_PAGE_SIZE) });
      if (previous?.next_created_at && previous.next_task_id) {
        query.set("before_created_at", previous.next_created_at);
        query.set("before_task_id", previous.next_task_id);
      }
      return `${openUrl}?${query}`;
    },
    [openUrl]
  );
  const open = useSWRInfinite<ExperimentOpenResponse>(getOpenKey, fetcher, {
    refreshInterval: (pages) => (pages?.[0]?.has_active_trials ? 30000 : 0),
    revalidateOnFocus: false,
    revalidateFirstPage: false,
    persistSize: true,
  });
  const experiment = open.data?.[0];

  const getTrialKey = useCallback(
    (pageIndex: number, previous: ExperimentTrialPageResponse | null) => {
      if (!trialPageUrl || (pageIndex > 0 && !previous?.next_trial_id))
        return null;
      const query = new URLSearchParams({ limit: String(TRIAL_PAGE_SIZE) });
      if (previous?.next_created_at && previous.next_trial_id) {
        query.set("before_created_at", previous.next_created_at);
        query.set("before_trial_id", previous.next_trial_id);
      }
      return `${trialPageUrl}?${query}`;
    },
    [trialPageUrl]
  );
  const trials = useSWRInfinite<ExperimentTrialPageResponse>(
    getTrialKey,
    fetcher,
    {
      refreshInterval: experiment?.has_active_trials ? 30000 : 0,
      revalidateOnFocus: false,
      revalidateFirstPage: false,
      persistSize: true,
    }
  );

  const lastOpenPage = open.data?.[open.data.length - 1];
  const lastTrialPage = trials.data?.[trials.data.length - 1];
  const hasMoreTasks = Boolean(lastOpenPage?.next_task_id);
  const hasMoreTrials = Boolean(lastTrialPage?.next_trial_id);
  const canLoadTasks = hasMoreTasks && !open.isLoading && !open.isValidating;
  const canLoadTrials =
    hasMoreTrials && !trials.isLoading && !trials.isValidating;
  const setOpenSize = open.setSize;
  const setTrialSize = trials.setSize;

  useEffect(() => {
    if (canLoadTasks) void setOpenSize((size) => size + 1);
  }, [canLoadTasks, setOpenSize]);
  useEffect(() => {
    if (canLoadTrials) void setTrialSize((size) => size + 1);
  }, [canLoadTrials, setTrialSize]);

  const tasks = useMemo(
    () => buildTasks(open.data, trials.data, publicView),
    [open.data, trials.data, publicView]
  );
  const trialsLoaded =
    trials.data?.reduce((sum, page) => sum + page.trials.length, 0) ?? 0;
  const totalTrials = experiment?.summary.trial_count ?? 0;

  return {
    experiment,
    tasks,
    openError: open.error,
    trialError: trials.error,
    isLoading: open.isLoading && !experiment,
    isLoadingPages:
      hasMoreTasks ||
      open.isValidating ||
      (totalTrials > 0 &&
        (trials.isLoading || trials.isValidating || hasMoreTrials)),
    trialsLoaded,
    totalTrials,
    trialsStalled: Boolean(trials.error) && trialsLoaded < totalTrials,
    isValidatingTrials: trials.isValidating,
    mutateOpen: open.mutate,
    mutateTrials: trials.mutate,
  };
}
