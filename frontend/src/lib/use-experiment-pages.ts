"use client";

import { useCallback, useEffect, useMemo } from "react";
import useSWRInfinite from "swr/infinite";
import { fetcher } from "@/lib/api";
import { buildExperimentTasks } from "@/lib/experiment-page-data";
import type {
  ExperimentOpenResponse,
  PublicExperimentOpenResponse,
  ExperimentTrialPageResponse,
} from "@/lib/types";

const OPEN_PAGE_SIZE = 100;
const TRIAL_PAGE_SIZE = 250;
const ACTIVE_REFRESH_INTERVAL_MS = 30000;

export function useExperimentPages({
  openUrl,
  trialPageUrl,
  publicView = false,
}: {
  openUrl: string | null;
  trialPageUrl: string | null;
  publicView?: boolean;
}) {
  type OpenResponse = ExperimentOpenResponse | PublicExperimentOpenResponse;
  const getOpenKey = useCallback(
    (pageIndex: number, previous: OpenResponse | null) => {
      if (!openUrl || (pageIndex > 0 && !previous?.next_task_id)) return null;
      const query = new URLSearchParams({ limit: String(OPEN_PAGE_SIZE) });
      if (pageIndex > 0) query.set("include_summary", "false");
      if (previous?.next_created_at && previous.next_task_id) {
        query.set("before_created_at", previous.next_created_at);
        query.set("before_task_id", previous.next_task_id);
      }
      return `${openUrl}?${query}`;
    },
    [openUrl]
  );
  const open = useSWRInfinite<OpenResponse>(getOpenKey, fetcher, {
    revalidateOnFocus: false,
    revalidateFirstPage: false,
    persistSize: false,
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
      revalidateOnFocus: false,
      revalidateFirstPage: false,
      persistSize: false,
      shouldRetryOnError: false,
    }
  );

  const lastOpenPage = open.data?.[open.data.length - 1];
  const lastTrialPage = trials.data?.[trials.data.length - 1];
  const hasMoreTasks = Boolean(lastOpenPage?.next_task_id);
  const hasMoreTrials = Boolean(lastTrialPage?.next_trial_id);
  const canLoadTasks =
    hasMoreTasks && !open.error && !open.isLoading && !open.isValidating;
  const canLoadTrials =
    hasMoreTrials && !trials.error && !trials.isLoading && !trials.isValidating;
  const {
    error: openError,
    isLoading: isLoadingOpen,
    isValidating: isValidatingOpen,
    mutate: mutateOpen,
    setSize: setOpenSize,
  } = open;
  const {
    error: trialError,
    isLoading: isLoadingTrials,
    isValidating: isValidatingTrials,
    mutate: mutateTrials,
    setSize: setTrialSize,
  } = trials;

  useEffect(() => {
    if (!experiment?.has_active_trials) return;

    const interval = window.setInterval(() => {
      void mutateOpen();
      void mutateTrials();
    }, ACTIVE_REFRESH_INTERVAL_MS);

    return () => window.clearInterval(interval);
  }, [experiment?.has_active_trials, mutateOpen, mutateTrials]);

  // Fetch one bounded page at a time, independently of the scroll position.
  // Wait for the requested size to arrive before advancing either cursor.
  const openPageCount = open.data?.length ?? 0;
  const trialPageCount = trials.data?.length ?? 0;
  const openSize = open.size;
  const trialSize = trials.size;
  useEffect(() => {
    if (canLoadTasks && openPageCount === openSize) {
      void setOpenSize(openSize + 1);
    }
  }, [canLoadTasks, openPageCount, openSize, setOpenSize]);
  useEffect(() => {
    if (canLoadTrials && trialPageCount === trialSize) {
      void setTrialSize(trialSize + 1);
    }
  }, [canLoadTrials, trialPageCount, trialSize, setTrialSize]);

  const retryTrials = useCallback(() => {
    if (isLoadingTrials || isValidatingTrials) return;
    void mutateTrials();
  }, [isLoadingTrials, isValidatingTrials, mutateTrials]);

  const tasks = useMemo(
    () => buildExperimentTasks(open.data, trials.data, publicView),
    [open.data, trials.data, publicView]
  );
  const trialsLoaded =
    trials.data?.reduce((sum, page) => sum + page.trials.length, 0) ?? 0;
  const totalTrials = experiment?.summary?.trial_count ?? 0;
  const trialPagesComplete =
    totalTrials === 0 ||
    Boolean(
      trials.data &&
      !hasMoreTrials &&
      !trialError &&
      trialsLoaded >= totalTrials
    );

  const pagesComplete = Boolean(
    experiment &&
    !openError &&
    !hasMoreTasks &&
    tasks.length >= (experiment.summary?.task_count ?? 0) &&
    trialPagesComplete
  );

  return {
    experiment,
    tasks,
    openError,
    trialError,
    isLoading: isLoadingOpen && !experiment,
    isLoadingTrials: totalTrials > 0 && (isLoadingTrials || isValidatingTrials),
    retryTrials,
    trialsLoaded,
    totalTrials,
    trialsStalled: Boolean(trialError),
    pagesComplete,
    isValidatingTrials,
    isValidatingOpen,
    mutateOpen,
    mutateTrials,
  };
}
