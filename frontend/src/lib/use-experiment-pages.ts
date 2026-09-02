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
    hasMoreTrials &&
    !trials.error &&
    !trials.isLoading &&
    !trials.isValidating;
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

    const firstOpenPageKey = getOpenKey(0, null);
    const firstTrialPageKey = getTrialKey(0, null);
    const interval = window.setInterval(() => {
      void mutateOpen(undefined, {
        revalidate: (_page, pageKey) => pageKey === firstOpenPageKey,
      });
      void mutateTrials(undefined, {
        revalidate: (_page, pageKey) => pageKey === firstTrialPageKey,
      });
    }, ACTIVE_REFRESH_INTERVAL_MS);

    return () => window.clearInterval(interval);
  }, [
    experiment?.has_active_trials,
    getOpenKey,
    getTrialKey,
    mutateOpen,
    mutateTrials,
  ]);

  const loadNextTasks = useCallback(() => {
    if (isLoadingOpen || isValidatingOpen) return;
    if (openError) {
      void mutateOpen();
      return;
    }
    if (hasMoreTasks) void setOpenSize((size) => size + 1);
  }, [
    hasMoreTasks,
    isLoadingOpen,
    isValidatingOpen,
    mutateOpen,
    openError,
    setOpenSize,
  ]);
  const loadNextTrials = useCallback(() => {
    if (trialError || isLoadingTrials || isValidatingTrials) return;
    if (hasMoreTrials) void setTrialSize((size) => size + 1);
  }, [
    hasMoreTrials,
    isLoadingTrials,
    isValidatingTrials,
    setTrialSize,
    trialError,
  ]);
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

  return {
    experiment,
    tasks,
    openError,
    trialError,
    isLoading: isLoadingOpen && !experiment,
    isLoadingTrials: totalTrials > 0 && (isLoadingTrials || isValidatingTrials),
    hasMoreTasks,
    hasMoreTrials,
    canLoadTasks,
    canLoadTrials,
    loadNextTasks,
    loadNextTrials,
    retryTrials,
    trialsLoaded,
    totalTrials,
    trialsStalled: Boolean(trialError) && trialsLoaded < totalTrials,
    trialPagesComplete,
    isValidatingTrials,
    mutateOpen,
    mutateTrials,
  };
}
