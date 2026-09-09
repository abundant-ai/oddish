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
  // Stream the trial pages in on their own. Each page that lands renders
  // immediately and, if it reports another cursor, queues the next request,
  // so results keep filling in instead of stopping at a "load more" click.
  // Requests stay strictly sequential: the size only grows once every page
  // already asked for has arrived.
  const loadedTrialPages = trials.data?.length ?? 0;
  const requestedTrialPages = trials.size;
  useEffect(() => {
    if (!hasMoreTrials || trialError) return;
    if (isLoadingTrials || isValidatingTrials) return;
    // A requested page is still in flight; advancing here would fan out
    // several pages at once.
    if (loadedTrialPages !== requestedTrialPages) return;
    void setTrialSize((size) => size + 1);
  }, [
    hasMoreTrials,
    isLoadingTrials,
    isValidatingTrials,
    loadedTrialPages,
    requestedTrialPages,
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
    loadNextTasks,
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
