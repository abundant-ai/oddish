"use client";

import {
  Suspense,
  use,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import useSWR from "swr";
import useSWRInfinite from "swr/infinite";
import { useAuth } from "@clerk/nextjs";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { ExperimentShareButton } from "@/components/experiment-share-button";
import { ChatButton } from "@/components/cc-chat/chat-button";
import {
  ProbeLaunchButton,
  resolveProbeHostTask,
} from "@/components/probe-launch-button";
import { ExperimentDetailView } from "@/components/experiment-detail-view";
import { ExperimentDescription } from "@/components/experiment-description";
import type {
  Task,
  Trial,
  ExperimentShareInfo,
  ExperimentCostTotals,
} from "@/lib/types";
import { fetcher } from "@/lib/api";
import { isOrgAdminRole } from "@/lib/org-roles";
import { Loader2, Pencil } from "lucide-react";
import { encodeExperimentRouteParam } from "@/lib/utils";
import {
  fetchFreshExperimentTaskPage,
  hasFatalExperimentTaskLoadError,
  mergeExperimentTaskPages,
} from "@/lib/experiment-task-pages";
import { ExperimentPageSkeleton } from "./experiment-skeleton";

// Paper-styled header action button, shared by the Probe and Chat buttons so
// they render as the same element.
const HEADER_ACTION_BUTTON_CLASS =
  "h-8 select-none gap-[7px] rounded-[7px] border border-[color:var(--paper-line)] bg-[color:var(--paper-surface)] px-3 text-[12px] leading-none text-[color:var(--paper-ink)] transition-colors hover:border-[color:var(--paper-ink-4)] hover:bg-[color:var(--paper-surface-2)]";

const TRIALS_BATCH_SIZE = 250;
const TRIALS_PREFETCH_PAGES = 2;
const EXPERIMENT_TIMING_STORAGE_KEY = "oddish:experiment-table-timing";
const ACTIVE_TASK_STATUSES = new Set([
  "pending",
  "queued",
  "running",
  "analyzing",
  "verdict_pending",
]);

function isExperimentTimingEnabled(): boolean {
  if (typeof window === "undefined") return false;
  const params = new URLSearchParams(window.location.search);
  return (
    params.get("debug_timing") === "1" ||
    window.localStorage.getItem(EXPERIMENT_TIMING_STORAGE_KEY) === "1"
  );
}

async function fetchExperimentTasksPage(url: string): Promise<Task[]> {
  const startedAt = performance.now();
  const res = await fetchFreshExperimentTaskPage(url);
  const responseAt = performance.now();
  const serverTiming = res.headers.get("server-timing");
  let data: unknown = null;

  try {
    data = await res.json();
  } catch {
    data = null;
  }

  const finishedAt = performance.now();
  if (isExperimentTimingEnabled()) {
    console.info("[oddish timing] experiment tasks fetch", {
      url,
      status: res.status,
      networkMs: Math.round(responseAt - startedAt),
      jsonMs: Math.round(finishedAt - responseAt),
      totalMs: Math.round(finishedAt - startedAt),
      rows: Array.isArray(data) ? data.length : null,
      serverTiming,
    });
  }

  if (!res.ok) {
    const message =
      typeof data === "object" && data && "error" in data
        ? String((data as { error?: string }).error)
        : res.statusText || "Request failed";
    const err = new Error(message);
    (err as Error & { status?: number; info?: unknown }).status = res.status;
    (err as Error & { status?: number; info?: unknown }).info = data;
    throw err;
  }

  return data as Task[];
}

type ExperimentClientPageProps = {
  experimentId: string;
  initialTasksPromise: Promise<Task[] | null>;
};

export function ExperimentClientPage({
  experimentId,
  initialTasksPromise,
}: ExperimentClientPageProps) {
  return (
    <Suspense key={experimentId} fallback={<ExperimentPageSkeleton />}>
      <ExperimentContent
        experimentId={experimentId}
        initialTasksPromise={initialTasksPromise}
      />
    </Suspense>
  );
}

function ExperimentContent({
  experimentId,
  initialTasksPromise,
}: ExperimentClientPageProps) {
  const initialTasks = use(initialTasksPromise);
  const { orgRole } = useAuth();

  const [isEditingName, setIsEditingName] = useState(false);
  const [nameDraft, setNameDraft] = useState("");
  const [nameError, setNameError] = useState<string | null>(null);
  const [isSavingName, setIsSavingName] = useState(false);
  const [copiedExperimentName, setCopiedExperimentName] = useState(false);
  const copiedExperimentNameTimeoutRef = useRef<number | null>(null);

  const encodedId = experimentId
    ? encodeExperimentRouteParam(experimentId)
    : "";

  // Phase 1: Fetch ALL tasks without trial data (lightweight).
  // Populates the full task list immediately. Uses the dedicated
  // ``task-shells`` endpoint, which drops the per-task ``experiments``
  // fan-out. Phase 2 below uses the compact ``slim-tasks`` endpoint.
  const allTasksUrl = experimentId
    ? `/api/experiments/${encodedId}/task-shells?limit=2000&offset=0`
    : null;

  const {
    data: lightweightTasks,
    error: lightweightError,
    isLoading: isLoadingTasks,
    mutate: mutateLightweight,
  } = useSWR<Task[]>(allTasksUrl, fetchExperimentTasksPage, {
    refreshInterval: 0,
    revalidateOnFocus: false,
    // A client-side revisit can otherwise prefer SWR's old shell over fresh
    // server fallback data after the task's default version changes.
    revalidateOnMount: true,
    revalidateIfStale: true,
    fallbackData: initialTasks ?? undefined,
  });

  // Phase 2: Progressively fetch compact trial data in batches.
  const getTrialsPageKey = useCallback(
    (pageIndex: number, previousPageData: Task[] | null) => {
      if (!experimentId || !encodedId) return null;
      if (previousPageData && previousPageData.length < TRIALS_BATCH_SIZE)
        return null;
      const offset = pageIndex * TRIALS_BATCH_SIZE;
      // Phase 2 uses the dedicated slim-tasks endpoint: tasks with trimmed
      // per-trial payloads (grid fields + cost). Full per-trial detail is
      // fetched on click via /api/trials/{id} (see ExperimentDetailView's
      // loadFullTrialOnOpen). The old /tasks proxy is left untouched.
      return `/api/experiments/${encodedId}/slim-tasks?limit=${TRIALS_BATCH_SIZE}&offset=${offset}`;
    },
    [experimentId, encodedId]
  );

  const {
    data: trialPages,
    isLoading: isLoadingTrialPages,
    isValidating: isValidatingTrials,
    size: trialsPageSize,
    setSize: setTrialsSize,
    mutate: mutateTrials,
  } = useSWRInfinite<Task[]>(getTrialsPageKey, fetchExperimentTasksPage, {
    refreshInterval: 0,
    revalidateOnFocus: false,
    revalidateFirstPage: false,
    revalidateOnMount: true,
    persistSize: true,
  });
  const trialsLastPage = trialPages?.[trialPages.length - 1] ?? null;
  const hasMoreTrials = Boolean(
    trialsLastPage && trialsLastPage.length === TRIALS_BATCH_SIZE
  );

  // What the experiment SPENT. Can't be derived from the trial pages above:
  // they're paginated (so a client-side sum only covers what's loaded), and
  // they're filtered to each task's current version (so they omit earlier
  // versions, superseded retries and probes, all of which were still billed).
  const costTotalsKey = experimentId
    ? `/api/experiments/${encodedId}/cost-totals`
    : null;
  const {
    data: costTotals,
    error: costTotalsError,
    mutate: mutateCostTotals,
  } = useSWR<ExperimentCostTotals>(costTotalsKey, fetcher, {
    refreshInterval: 0,
    revalidateOnFocus: false,
  });
  // In flight. The tiles must not fall back to the client sum meanwhile: that
  // number is wrong on two axes (loaded pages only, grid-filtered) and would
  // visibly jump when the real total lands. Show a placeholder instead. On
  // error we do fall back, so a failed rollup degrades rather than blanks.
  const costTotalsPending =
    costTotalsKey != null && costTotals === undefined && !costTotalsError;

  // Experiment-level metadata (sharing + description) for the header.
  // Fetched eagerly so the description renders immediately; shares the SWR
  // cache key with ExperimentShareButton (which fetches lazily on open).
  const experimentShareKey = experimentId
    ? `/api/experiments/${encodedId}/share`
    : null;
  const { data: experimentShare, mutate: mutateExperimentShare } =
    useSWR<ExperimentShareInfo>(experimentShareKey, fetcher, {
      revalidateOnFocus: false,
    });

  // Merge lightweight task shells with trial-enriched data. The backend scopes
  // trials and counts to the experiment-relevant version while always
  // reporting the task's selected default as ``current_version``.
  const tasksForExperiment = useMemo(() => {
    const startedAt = isExperimentTimingEnabled() ? performance.now() : 0;
    const merged = mergeExperimentTaskPages(lightweightTasks, trialPages);

    if (isExperimentTimingEnabled()) {
      const enrichedIds = new Set(
        (trialPages ?? []).flatMap((page) =>
          (page ?? []).map((task) => task.id)
        )
      );
      console.info("[oddish timing] experiment task merge", {
        baseRows: lightweightTasks?.length ?? 0,
        enrichedRows: enrichedIds.size,
        mergedRows: merged.length,
        mergeMs: Math.round(performance.now() - startedAt),
      });
    }
    return merged;
  }, [lightweightTasks, trialPages]);
  const hasFatalTaskLoadError = hasFatalExperimentTaskLoadError(
    lightweightError,
    tasksForExperiment
  );

  const probeHostTask = useMemo(
    () => resolveProbeHostTask(tasksForExperiment),
    [tasksForExperiment]
  );

  const isLoading = isLoadingTasks;
  const isLoadingTrials =
    (lightweightTasks?.length ?? 0) > 0 &&
    (isLoadingTrialPages || isValidatingTrials);
  const trialsLoadedCount = useMemo(() => {
    if (!trialPages) return 0;
    return trialPages.reduce((sum, page) => sum + (page?.length ?? 0), 0);
  }, [trialPages]);
  const totalTaskCount = lightweightTasks?.length ?? 0;
  const remainingTrialTaskCount = Math.max(
    0,
    totalTaskCount - trialsLoadedCount
  );
  const canLoadMoreTrials =
    hasMoreTrials && !isLoadingTrialPages && !isValidatingTrials;
  const canLoadAllTrials =
    totalTaskCount > 0 &&
    remainingTrialTaskCount > 0 &&
    !isLoadingTrialPages &&
    !isValidatingTrials;

  const refreshIntervalMs = useMemo(() => {
    if (tasksForExperiment.length === 0) return 5000;
    const hasActiveTasks = tasksForExperiment.some((task) => {
      // Subtract skipped: skipped trials are terminal (never ran), so they must
      // not read as "active" — otherwise a done, gate-skipped task would poll at
      // the fast interval forever.
      const activeTrials = Math.max(
        0,
        task.total - task.completed - task.failed - (task.skipped ?? 0)
      );
      return activeTrials > 0 || ACTIVE_TASK_STATUSES.has(task.status);
    });
    return hasActiveTasks ? 30000 : 90000;
  }, [tasksForExperiment]);

  const experimentName = tasksForExperiment[0]?.experiment_name ?? "";
  const displayName = experimentName || experimentId || "Experiment";
  const initialName = experimentName || experimentId || "";
  const canManageExperimentShare = isOrgAdminRole(orgRole);

  // Deletes below write the grid optimistically, so for one round trip the row
  // is gone while the cost tiles still show the pre-delete rollup. Do NOT
  // "fix" that by optimistically subtracting the removed trials' cost: the only
  // trials on the client are the ones the grid renders, and the rollup also
  // counts that task's probes, superseded retries and earlier-version trials.
  // Subtracting the visible ones would leave the tile too LOW -- a spend number
  // derived from the visible rows, which is the bug this endpoint exists to
  // remove. Refetching is the correct (and self-healing) answer.
  const refreshTaskPages = useCallback(
    async (_taskIds?: string[]) => {
      await Promise.all([
        mutateLightweight(),
        mutateTrials(),
        mutateCostTotals(),
      ]);
    },
    [mutateLightweight, mutateTrials, mutateCostTotals]
  );

  const loadMoreTrials = useCallback(() => {
    if (!canLoadMoreTrials) return;
    void setTrialsSize((size) => size + 1);
  }, [canLoadMoreTrials, setTrialsSize]);

  const loadAllTrials = useCallback(() => {
    if (!canLoadAllTrials || totalTaskCount === 0) return;
    void setTrialsSize(Math.ceil(totalTaskCount / TRIALS_BATCH_SIZE));
  }, [canLoadAllTrials, setTrialsSize, totalTaskCount]);

  useEffect(() => {
    if (!canLoadMoreTrials || isLoadingTrialPages || isValidatingTrials) return;
    if (trialsPageSize >= TRIALS_PREFETCH_PAGES) return;
    void setTrialsSize((size) => Math.min(size + 1, TRIALS_PREFETCH_PAGES));
  }, [
    canLoadMoreTrials,
    isLoadingTrialPages,
    isValidatingTrials,
    trialsPageSize,
    setTrialsSize,
  ]);

  useEffect(() => {
    if (!isExperimentTimingEnabled() || tasksForExperiment.length === 0) return;
    const frame = window.requestAnimationFrame(() => {
      console.info("[oddish timing] experiment table first paint candidate", {
        tasks: tasksForExperiment.length,
        trialPages: trialPages?.length ?? 0,
        trialsLoadedCount,
        sinceNavigationMs: Math.round(performance.now()),
      });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [tasksForExperiment.length, trialPages?.length, trialsLoadedCount]);

  useEffect(() => {
    if (!isEditingName) {
      setNameDraft(initialName);
      setNameError(null);
    }
  }, [initialName, isEditingName]);

  useEffect(() => {
    setCopiedExperimentName(false);
    if (copiedExperimentNameTimeoutRef.current !== null) {
      window.clearTimeout(copiedExperimentNameTimeoutRef.current);
      copiedExperimentNameTimeoutRef.current = null;
    }
  }, [displayName]);

  useEffect(() => {
    return () => {
      if (copiedExperimentNameTimeoutRef.current !== null) {
        window.clearTimeout(copiedExperimentNameTimeoutRef.current);
      }
    };
  }, []);

  useEffect(() => {
    if (!allTasksUrl) return;

    const intervalId = window.setInterval(() => {
      void mutateLightweight();
      // Refresh every trial page that's been loaded so far -- not
      // just the first. Polling only the first page caused later
      // pages to age (e.g. trial status badges going stale on row
      // 251+ until the user manually triggered a re-fetch).
      // ``mutateTrials()`` re-runs every page key currently held by
      // useSWRInfinite, in order, with the regular SWR dedup window.
      void mutateTrials();
      // Cheap grouped aggregate; refresh alongside so the cost tiles track
      // trials finishing while the page is open.
      void mutateCostTotals();
    }, refreshIntervalMs);

    return () => {
      window.clearInterval(intervalId);
    };
  }, [
    allTasksUrl,
    refreshIntervalMs,
    mutateLightweight,
    mutateTrials,
    mutateCostTotals,
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
        }
      );

      if (!res.ok) {
        const errorData = await res.json().catch(() => ({}));
        throw new Error(
          errorData.detail || errorData.error || "Failed to rename experiment"
        );
      }

      setIsEditingName(false);
      await mutateLightweight(
        (tasks) =>
          tasks?.map((task) => ({ ...task, experiment_name: nextName })),
        { revalidate: false }
      );
      await mutateTrials(
        (pages) =>
          pages?.map((page) =>
            page?.map((task) => ({ ...task, experiment_name: nextName }))
          ),
        { revalidate: false }
      );
      void refreshTaskPages();
    } catch (err) {
      setNameError(err instanceof Error ? err.message : "Rename failed");
    } finally {
      setIsSavingName(false);
    }
  };

  const handleUnlinkTask = async (task: Task) => {
    const res = await fetch(
      `/api/experiments/${encodedId}/tasks/${encodeURIComponent(task.id)}`,
      {
        method: "DELETE",
      }
    );

    if (!res.ok) {
      const errorData = await res.json().catch(() => ({}));
      throw new Error(
        errorData.detail ||
          errorData.error ||
          "Failed to unlink task from experiment"
      );
    }

    await mutateLightweight(
      (tasks) => tasks?.filter((item) => item.id !== task.id),
      { revalidate: false }
    );
    await mutateTrials(
      (pages) =>
        pages?.map((page) => page?.filter((item) => item.id !== task.id)),
      { revalidate: false }
    );
    await refreshTaskPages();
  };

  const handleDeleteTrial = async (trial: Trial, _task: Task | null) => {
    const res = await fetch(`/api/trials/${encodeURIComponent(trial.id)}`, {
      method: "DELETE",
    });

    if (!res.ok) {
      const errorData = await res.json().catch(() => ({}));
      throw new Error(
        errorData.detail || errorData.error || "Failed to delete trial"
      );
    }

    const filterTrials = (tasks: Task[] | undefined) =>
      tasks?.map((task) =>
        task.trials?.some((t) => t.id === trial.id)
          ? { ...task, trials: task.trials.filter((t) => t.id !== trial.id) }
          : task
      );

    await mutateLightweight(filterTrials, { revalidate: false });
    await mutateTrials(
      (pages) => pages?.map((page) => filterTrials(page) ?? page),
      { revalidate: false }
    );
    await refreshTaskPages();
  };

  const handleCopyExperimentName = async () => {
    await navigator.clipboard.writeText(displayName);
    setCopiedExperimentName(true);
    if (copiedExperimentNameTimeoutRef.current !== null) {
      window.clearTimeout(copiedExperimentNameTimeoutRef.current);
    }
    copiedExperimentNameTimeoutRef.current = window.setTimeout(() => {
      setCopiedExperimentName(false);
      copiedExperimentNameTimeoutRef.current = null;
    }, 2000);
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
          costTotals={costTotals}
          costTotalsPending={costTotalsPending}
          isLoading={isLoading}
          isLoadingTrials={isLoadingTrials}
          // SWR retains successful fallback/revalidation data when a later
          // request fails. Keep that usable grid visible instead of replacing
          // it with the fatal error state during a transient backend failure.
          hasError={hasFatalTaskLoadError}
          loadFullTrialOnOpen
          headerLeft={
            isEditingName ? (
              <div className="flex flex-wrap items-center gap-2">
                <Input
                  value={nameDraft}
                  onChange={(event) => setNameDraft(event.target.value)}
                  className="h-10 w-[320px] border-[color:var(--paper-line)] bg-[color:var(--paper-surface)] font-mono text-[22px] font-semibold tracking-[-0.02em]"
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
              <div className="flex min-w-0 items-center gap-2">
                <Button
                  type="button"
                  variant="ghost"
                  onClick={handleCopyExperimentName}
                  className="h-auto max-w-full min-w-0 cursor-pointer justify-start truncate rounded-sm bg-transparent p-0 pb-1 text-left font-mono text-[26px] leading-[1.25] font-semibold tracking-[-0.02em] text-[color:var(--paper-ink)] transition hover:bg-transparent hover:text-[color:var(--paper-ink-2)]"
                  aria-label={`Copy experiment name ${displayName}`}
                  title={
                    copiedExperimentName
                      ? "Copied"
                      : "Click to copy experiment name"
                  }
                >
                  <h1 className="truncate">{displayName}</h1>
                </Button>
                {copiedExperimentName && (
                  <span
                    aria-live="polite"
                    className="font-mono text-[11px] text-[color:var(--paper-ink-3)]"
                  >
                    copied
                  </span>
                )}
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  onClick={() => setIsEditingName(true)}
                  disabled={!experimentId}
                  className="h-6 w-6 rounded-sm text-[color:var(--paper-ink-3)] transition hover:bg-[color:var(--paper-surface-2)] hover:text-[color:var(--paper-ink)] disabled:opacity-50"
                  aria-label="Rename experiment"
                  title="Rename experiment"
                >
                  <Pencil className="h-3.5 w-3.5" />
                </Button>
              </div>
            )
          }
          headerStatus={
            isLoadingTrials ? (
              <div className="text-muted-foreground flex items-center gap-1.5 text-[10px]">
                <Loader2 className="h-3 w-3 animate-spin" />
                <span>
                  Loading trials
                  {lightweightTasks
                    ? ` ${trialsLoadedCount}/${lightweightTasks.length}`
                    : ""}
                  …
                </span>
              </div>
            ) : null
          }
          headerRight={
            experimentId ? (
              <div className="flex items-center gap-2">
                {probeHostTask ? (
                  <ProbeLaunchButton
                    taskId={probeHostTask.id}
                    taskName={probeHostTask.name}
                    variant="labeled"
                    label="Probe"
                    className={HEADER_ACTION_BUTTON_CLASS}
                  />
                ) : null}
                <ChatButton
                  scopeKind="experiment"
                  scopeId={experimentId}
                  variant="ghost"
                  className={HEADER_ACTION_BUTTON_CLASS}
                />
                <ExperimentShareButton
                  experimentId={experimentId}
                  canManageShare={canManageExperimentShare}
                />
              </div>
            ) : null
          }
          headerDescription={
            experimentId ? (
              <ExperimentDescription
                experimentId={experimentId}
                description={experimentShare?.description ?? null}
                onSaved={(next) =>
                  void mutateExperimentShare(
                    (prev) => (prev ? { ...prev, description: next } : prev),
                    { revalidate: false }
                  )
                }
              />
            ) : null
          }
          inlineAlert={
            nameError ? (
              <Alert variant="destructive">
                <AlertTitle>Rename failed</AlertTitle>
                <AlertDescription>{nameError}</AlertDescription>
              </Alert>
            ) : lightweightError && tasksForExperiment.length > 0 ? (
              <Alert>
                <AlertTitle>Could not refresh experiment</AlertTitle>
                <AlertDescription>
                  Showing the most recently loaded task data.
                </AlertDescription>
              </Alert>
            ) : null
          }
          tableFooter={
            remainingTrialTaskCount > 0 ? (
              <Alert>
                <AlertTitle>Trial details are loading on demand</AlertTitle>
                <AlertDescription className="flex flex-wrap items-center gap-2">
                  <span>
                    Loaded compact trial data for {trialsLoadedCount}/
                    {totalTaskCount} tasks.
                  </span>
                  <Button
                    type="button"
                    variant="secondary"
                    size="sm"
                    className="h-7"
                    onClick={loadMoreTrials}
                    disabled={!canLoadMoreTrials}
                  >
                    Load next{" "}
                    {Math.min(TRIALS_BATCH_SIZE, remainingTrialTaskCount)}
                  </Button>
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    className="h-7"
                    onClick={loadAllTrials}
                    disabled={!canLoadAllTrials}
                  >
                    Load all
                  </Button>
                </AlertDescription>
              </Alert>
            ) : null
          }
          readOnly={false}
          allowRetry
          onTaskUnlink={handleUnlinkTask}
          onTrialDelete={handleDeleteTrial}
          onRerun={refreshTaskPages}
        />
      )}
    </div>
  );
}
