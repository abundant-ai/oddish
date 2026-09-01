"use client";

import {
  Suspense,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import Link from "next/link";
import useSWR from "swr";
import useSWRInfinite from "swr/infinite";
import { useAuth } from "@clerk/nextjs";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { ExperimentShareButton } from "@/components/experiment-share-button";
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
  ExperimentOpenResponse,
  ExperimentTrialCell,
  ExperimentTrialPageResponse,
} from "@/lib/types";
import { fetcher } from "@/lib/api";
import { isOrgAdminRole } from "@/lib/org-roles";
import { Loader2, Pencil } from "lucide-react";
import { encodeExperimentRouteParam } from "@/lib/utils";
import { ExperimentPageSkeleton } from "@/components/experiment-page-skeleton";

// Shared by the experiment header action buttons so they render identically.
const HEADER_ACTION_BUTTON_CLASS =
  "h-8 select-none gap-[7px] rounded-[7px] border border-[color:var(--paper-line)] bg-[color:var(--paper-surface)] px-3 text-[12px] leading-none text-[color:var(--paper-ink)] transition-colors hover:border-[color:var(--paper-ink-4)] hover:bg-[color:var(--paper-surface-2)]";

const TRIALS_BATCH_SIZE = 250;

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

function buildExperimentTasks(
  openPages: ExperimentOpenResponse[] | undefined,
  trialPages: ExperimentTrialPageResponse[] | undefined
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
      experiment_is_public: false,
      experiment_created_at: experiment.created_at,
      experiment_owner: experiment.owner,
      experiment_link: experiment.link,
      trials: trialsByTask.get(task.id),
    }))
  );
}

type ExperimentClientPageProps = {
  experimentId: string;
};

export function ExperimentClientPage({
  experimentId,
}: ExperimentClientPageProps) {
  return (
    // The Suspense boundary is required because ExperimentDetailView uses
    // useSearchParams, which needs one during prerendering. The key causes
    // everything inside to remount when the experiment changes, so no
    // state carries over from one experiment to another.
    <Suspense key={experimentId} fallback={<ExperimentPageSkeleton />}>
      <ExperimentContent experimentId={experimentId} />
    </Suspense>
  );
}

function ExperimentContent({ experimentId }: ExperimentClientPageProps) {
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

  const getOpenPageKey = useCallback(
    (pageIndex: number, previous: ExperimentOpenResponse | null) => {
      if (!encodedId || (pageIndex > 0 && !previous?.next_task_id)) return null;
      const query = new URLSearchParams({ limit: "100" });
      if (previous?.next_created_at && previous.next_task_id) {
        query.set("before_created_at", previous.next_created_at);
        query.set("before_task_id", previous.next_task_id);
      }
      return `/api/experiments/${encodedId}/open?${query}`;
    },
    [encodedId]
  );
  const {
    data: openPages,
    error: openError,
    isLoading: isLoadingOpen,
    isValidating: isValidatingOpen,
    setSize: setOpenSize,
    mutate: mutateOpen,
  } = useSWRInfinite<ExperimentOpenResponse>(getOpenPageKey, fetcher, {
    refreshInterval: (pages) => (pages?.[0]?.has_active_trials ? 30000 : 0),
    revalidateOnFocus: false,
    revalidateFirstPage: false,
    persistSize: true,
  });
  const experimentOpen = openPages?.[0];
  const openLastPage = openPages?.[openPages.length - 1];
  const hasMoreTasks = Boolean(openLastPage?.next_task_id);

  const getTrialsPageKey = useCallback(
    (pageIndex: number, previous: ExperimentTrialPageResponse | null) => {
      if (!encodedId || (pageIndex > 0 && !previous?.next_trial_id))
        return null;
      const query = new URLSearchParams({ limit: String(TRIALS_BATCH_SIZE) });
      if (previous?.next_created_at && previous.next_trial_id) {
        query.set("before_created_at", previous.next_created_at);
        query.set("before_trial_id", previous.next_trial_id);
      }
      return `/api/experiments/${encodedId}/trial-page?${query}`;
    },
    [encodedId]
  );

  const {
    data: trialPages,
    error: trialsError,
    isLoading: isLoadingTrialPages,
    isValidating: isValidatingTrials,
    setSize: setTrialsSize,
    mutate: mutateTrials,
  } = useSWRInfinite<ExperimentTrialPageResponse>(getTrialsPageKey, fetcher, {
    refreshInterval: experimentOpen?.has_active_trials ? 30000 : 0,
    revalidateOnFocus: false,
    revalidateFirstPage: false,
    persistSize: true,
  });
  const trialsLastPage = trialPages?.[trialPages.length - 1] ?? null;
  const hasMoreTrials = Boolean(trialsLastPage?.next_trial_id);

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
    refreshInterval: experimentOpen?.has_active_trials ? 30000 : 0,
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

  const tasksForExperiment = useMemo(
    () => buildExperimentTasks(openPages, trialPages),
    [openPages, trialPages]
  );
  const hasFatalTaskLoadError = Boolean(openError) && !experimentOpen;

  const probeHostTask = useMemo(
    () => resolveProbeHostTask(tasksForExperiment),
    [tasksForExperiment]
  );

  const isLoading = isLoadingOpen && !experimentOpen;
  const isLoadingTrials =
    isValidatingOpen ||
    ((experimentOpen?.summary.trial_count ?? 0) > 0 &&
      (isLoadingTrialPages || isValidatingTrials));
  const trialsLoadedCount =
    trialPages?.reduce((sum, page) => sum + page.trials.length, 0) ?? 0;
  const totalTrialCount = experimentOpen?.summary.trial_count ?? 0;
  const trialsStalled =
    Boolean(trialsError) && trialsLoadedCount < totalTrialCount;

  const experimentName = experimentOpen?.name ?? "";
  const displayName = experimentName || experimentId || "Experiment";
  const initialName = experimentName || experimentId || "";
  const canManageExperimentShare = isOrgAdminRole(orgRole);
  // The qa-report experiment is QA's machinery, not a product surface: the
  // verdict, reasoning, and per-trial grades are all inline on this page and
  // in the task overview. Only admins get the hop, for debugging QA itself.
  const canSeeQaReport = isOrgAdminRole(orgRole);

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
      await Promise.all([mutateOpen(), mutateTrials(), mutateCostTotals()]);
    },
    [mutateOpen, mutateTrials, mutateCostTotals]
  );

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
    if (isLoadingTrialPages || isValidatingTrials) return;
    if (trialsError) {
      // Preserve the current SWRInfinite size so this retries the failed key
      // from the last successful page's cursor instead of skipping a page.
      void mutateTrials();
      return;
    }
    if (hasMoreTrials) void setTrialsSize((size) => size + 1);
  }, [
    hasMoreTrials,
    isLoadingTrialPages,
    isValidatingTrials,
    mutateTrials,
    setTrialsSize,
    trialsError,
  ]);

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
      await mutateOpen(
        (pages) => pages?.map((page) => ({ ...page, name: nextName })),
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

    await mutateOpen(
      (pages) =>
        pages?.map((page) => ({
          ...page,
          tasks: page.tasks.filter((item) => item.id !== task.id),
        })),
      { revalidate: false }
    );
    await mutateTrials(
      (pages) =>
        pages?.map((page) => ({
          ...page,
          trials: page.trials.filter((item) => item.task_id !== task.id),
        })),
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

    await mutateTrials(
      (pages) =>
        pages?.map((page) => ({
          ...page,
          trials: page.trials.filter((item) => item.id !== trial.id),
        })),
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
          pageSummary={experimentOpen?.summary}
          costTotals={costTotals}
          costTotalsPending={costTotalsPending}
          isLoading={isLoading}
          isLoadingTrials={isLoadingTrials}
          hasMoreTasks={hasMoreTasks}
          hasMoreTrials={hasMoreTrials}
          loadNextTasks={loadNextTasks}
          loadNextTrials={loadNextTrials}
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
                  {experimentOpen
                    ? ` ${trialsLoadedCount}/${totalTrialCount}`
                    : ""}
                  …
                </span>
              </div>
            ) : experimentShare?.shadow_of && canSeeQaReport ? (
              <Link
                href={`/experiments/${encodeExperimentRouteParam(experimentShare.shadow_of)}`}
                className="text-muted-foreground text-[10px] hover:underline"
                title="This page is the QA machinery for the graded experiment"
              >
                ⇄ graded experiment
              </Link>
            ) : experimentShare?.qa_report_experiment_id && canSeeQaReport ? (
              <Link
                href={`/experiments/${encodeExperimentRouteParam(experimentShare.qa_report_experiment_id)}`}
                className="text-muted-foreground text-[10px] hover:underline"
                title="Debug view: the QA/audit runs behind this experiment's verdicts"
              >
                ⇄ QA report
              </Link>
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
            ) : trialsStalled ? (
              // Outranks the refresh alert below: this one carries the only
              // recovery control.
              <Alert variant="destructive">
                <AlertTitle>Some trial results failed to load</AlertTitle>
                <AlertDescription className="flex flex-wrap items-center gap-2">
                  <span>
                    Loaded {trialsLoadedCount}/{totalTrialCount} trials.
                  </span>
                  <Button
                    type="button"
                    variant="secondary"
                    size="sm"
                    className="h-7"
                    onClick={loadNextTrials}
                    disabled={isValidatingTrials}
                  >
                    Retry
                  </Button>
                </AlertDescription>
              </Alert>
            ) : openError && tasksForExperiment.length > 0 ? (
              <Alert>
                <AlertTitle>Could not refresh experiment</AlertTitle>
                <AlertDescription>
                  Showing the most recently loaded task data.
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
