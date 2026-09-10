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
    controller: AbortController;
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
      const snapshot = {
        url: requestUrl,
        controller,
        results: {
          experiment: {
            ...latest.experiment,
            tasks: [...latest.experiment.tasks],
          },
          trials: [...latest.trials],
        },
      };
      // A replacement stream starts with empty rows. Retain the previous
      // snapshot until SWR can replace it with the completed response.
      setProgress((previous) =>
        previous?.url === requestUrl && previous.controller !== controller
          ? previous
          : snapshot
      );
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
    if (!hasActiveTrials) return;
    const interval = window.setInterval(() => {
      if (!active.current) void mutate();
    }, 30_000);
    return () => window.clearInterval(interval);
  }, [hasActiveTrials, mutate]);
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
    pagesComplete: !!data,
    refreshResults: mutate,
  };
}
