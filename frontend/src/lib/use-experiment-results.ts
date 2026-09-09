"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import useSWR from "swr";
import { apiFetch } from "@/lib/api";
import { buildExperimentTasks } from "@/lib/experiment-page-data";
import {
  readExperimentResults,
  type ExperimentResults,
} from "@/lib/experiment-results-stream";

export function useExperimentResults({
  url,
  publicView = false,
}: {
  url: string | null;
  publicView?: boolean;
}) {
  const [progress, setProgress] = useState<{
    url: string;
    results: ExperimentResults;
  }>();
  const active = useRef<{ url: string; controller: AbortController } | null>(
    null
  );
  const fetchResults = useCallback(async (requestUrl: string) => {
    active.current?.controller.abort();
    const controller = new AbortController();
    active.current = { url: requestUrl, controller };
    let frame: number | undefined;
    let latest: ExperimentResults | undefined;
    const publish = () => {
      frame = undefined;
      if (!latest || controller.signal.aborted) return;
      setProgress({
        url: requestUrl,
        results: {
          experiment: {
            ...latest.experiment,
            tasks: [...latest.experiment.tasks],
          },
          trials: [...latest.trials],
        },
      });
    };
    try {
      const response = await apiFetch(requestUrl, {
        signal: controller.signal,
      });
      return await readExperimentResults(response, (results) => {
        latest = results;
        // Paint incoming records once per browser frame, not once per network page.
        frame ??= requestAnimationFrame(publish);
      });
    } finally {
      if (frame != null) cancelAnimationFrame(frame);
      publish();
      if (active.current?.controller === controller) active.current = null;
    }
  }, []);
  const { data, error, isValidating, mutate } = useSWR(url, fetchResults, {
    revalidateOnFocus: false,
    shouldRetryOnError: false,
  });
  useEffect(
    () => () => {
      if (active.current?.url === url) active.current.controller.abort();
    },
    [url]
  );
  const hasActiveTrials = data?.experiment.has_active_trials ?? false;
  useEffect(() => {
    if (!hasActiveTrials || error) return;
    const interval = window.setInterval(() => {
      if (!active.current) void mutate();
    }, 30_000);
    return () => window.clearInterval(interval);
  }, [hasActiveTrials, error, mutate]);
  // Keep a completed snapshot visible during refresh; only the initial load paints progressively.
  const results =
    data ?? (progress?.url === url ? progress.results : undefined);
  const tasks = useMemo(
    () =>
      buildExperimentTasks(
        results ? [results.experiment] : undefined,
        results
          ? [{ revision: results.experiment.revision, trials: results.trials }]
          : undefined,
        publicView
      ),
    [results, publicView]
  );
  return {
    experiment: results?.experiment,
    tasks,
    error,
    isLoading: !!url && !results && !error,
    isLoadingTrials: isValidating,
    trialsLoaded: results?.trials.length ?? 0,
    totalTrials: results?.experiment.summary?.trial_count ?? 0,
    pagesComplete: !!data && !error,
    refreshResults: mutate,
  };
}
