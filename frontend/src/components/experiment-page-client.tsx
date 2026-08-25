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
import { useAuth } from "@clerk/nextjs";
import { Loader2, Pencil } from "lucide-react";
import useSWR from "swr";
import useSWRInfinite from "swr/infinite";

import { ExperimentDescription } from "@/components/experiment-description";
import { ExperimentDetailView } from "@/components/experiment-detail-view";
import { ExperimentPageSkeleton } from "@/components/experiment-page-skeleton";
import { ExperimentShareButton } from "@/components/experiment-share-button";
import {
  ProbeLaunchButton,
  resolveProbeHostTask,
} from "@/components/probe-launch-button";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { fetcher } from "@/lib/api";
import {
  fetchFreshExperimentTaskPage,
  mergeExperimentTaskPages,
} from "@/lib/experiment-task-pages";
import { isOrgAdminRole } from "@/lib/org-roles";
import type {
  ExperimentCostTotals,
  ExperimentOpenResponse,
  ExperimentRevisionResponse,
  ExperimentShareInfo,
  ExperimentTrialPageResponse,
  Task,
  Trial,
} from "@/lib/types";
import { encodeExperimentRouteParam } from "@/lib/utils";

export type ExperimentPageAccess =
  | { kind: "member"; experimentId: string }
  | { kind: "public"; token: string };

const HEADER_ACTION_BUTTON_CLASS =
  "h-8 select-none gap-[7px] rounded-[7px] border border-[color:var(--paper-line)] bg-[color:var(--paper-surface)] px-3 text-[12px] leading-none text-[color:var(--paper-ink)] transition-colors hover:border-[color:var(--paper-ink-4)] hover:bg-[color:var(--paper-surface-2)]";

async function fetchExperimentResource<T>(url: string): Promise<T> {
  const response = await fetchFreshExperimentTaskPage(url);
  const body = (await response.json().catch(() => null)) as
    | T
    | { detail?: string; error?: string }
    | null;
  if (!response.ok) {
    const message =
      body && typeof body === "object" && "detail" in body
        ? body.detail
        : body && typeof body === "object" && "error" in body
          ? body.error
          : response.statusText;
    throw new Error(message || "Experiment request failed");
  }
  return body as T;
}

function accessKey(access: ExperimentPageAccess): string {
  return access.kind === "member"
    ? `member:${access.experimentId}`
    : `public:${access.token}`;
}

export function ExperimentPageClient({
  access,
}: {
  access: ExperimentPageAccess;
}) {
  return (
    <Suspense key={accessKey(access)} fallback={<ExperimentPageSkeleton />}>
      {access.kind === "member" ? (
        <MemberExperimentPageContent access={access} />
      ) : (
        <ExperimentPageContent access={access} orgRole={null} />
      )}
    </Suspense>
  );
}

function MemberExperimentPageContent({
  access,
}: {
  access: Extract<ExperimentPageAccess, { kind: "member" }>;
}) {
  const { orgRole } = useAuth();
  return <ExperimentPageContent access={access} orgRole={orgRole} />;
}

function ExperimentPageContent({
  access,
  orgRole,
}: {
  access: ExperimentPageAccess;
  orgRole: string | null | undefined;
}) {
  const isPublic = access.kind === "public";
  const canManage = access.kind === "member" && isOrgAdminRole(orgRole);
  const resourceId =
    access.kind === "member"
      ? encodeExperimentRouteParam(access.experimentId)
      : encodeURIComponent(access.token);
  const apiBase = isPublic
    ? `/api/public/experiments/${resourceId}`
    : `/api/experiments/${resourceId}`;

  const getOpenPageKey = useCallback(
    (
      pageIndex: number,
      previousPage: ExperimentOpenResponse | null
    ): string | null => {
      if (pageIndex === 0) return `${apiBase}/open`;
      if (!previousPage?.next_cursor) return null;
      return `${apiBase}/open?cursor=${encodeURIComponent(previousPage.next_cursor)}`;
    },
    [apiBase]
  );
  const {
    data: openPages,
    error: openError,
    isLoading: isLoadingOpen,
    isValidating: isValidatingOpen,
    setSize: setOpenSize,
    mutate: mutateOpenPages,
  } = useSWRInfinite<ExperimentOpenResponse>(
    getOpenPageKey,
    fetchExperimentResource,
    {
      revalidateOnFocus: false,
      revalidateFirstPage: false,
      persistSize: true,
    }
  );
  const open = openPages?.[0];
  const lastOpenPage = openPages?.[openPages.length - 1];

  const taskShells = useMemo(() => {
    const byId = new Map<string, Task>();
    for (const page of openPages ?? []) {
      for (const task of page.tasks) byId.set(task.id, task);
    }
    return [...byId.values()];
  }, [openPages]);

  const getTrialPageKey = useCallback(
    (
      pageIndex: number,
      previousPage: ExperimentTrialPageResponse | null
    ): string | null => {
      if (!open) return null;
      if (pageIndex === 0) return `${apiBase}/trial-page`;
      if (!previousPage?.next_cursor) return null;
      return `${apiBase}/trial-page?cursor=${encodeURIComponent(previousPage.next_cursor)}`;
    },
    [apiBase, open]
  );
  const {
    data: trialResponses,
    error: trialError,
    isLoading: isLoadingTrialPages,
    isValidating: isValidatingTrials,
    setSize: setTrialSize,
    mutate: mutateTrialPages,
  } = useSWRInfinite<ExperimentTrialPageResponse>(
    getTrialPageKey,
    fetchExperimentResource,
    {
      revalidateOnFocus: false,
      revalidateFirstPage: false,
      persistSize: true,
    }
  );
  const firstTrialPage = trialResponses?.[0];
  const lastTrialPage = trialResponses?.[trialResponses.length - 1];
  const trialTaskPages = useMemo(
    () => trialResponses?.map((page) => page.tasks),
    [trialResponses]
  );
  const tasksForExperiment = useMemo(
    () => mergeExperimentTaskPages(taskShells, trialTaskPages),
    [taskShells, trialTaskPages]
  );

  const costKey =
    access.kind === "member" && firstTrialPage
      ? `${apiBase}/cost-totals`
      : null;
  const {
    data: costTotals,
    error: costError,
    mutate: mutateCostTotals,
  } = useSWR<ExperimentCostTotals>(costKey, fetcher, {
    refreshInterval: 0,
    revalidateOnFocus: false,
  });
  const costTotalsPending =
    costKey !== null && costTotals === undefined && !costError;

  // `/open` may truncate the description to protect the first-paint byte
  // budget. The authenticated `/share` resource owns editable metadata and
  // therefore loads independently of trial pagination.
  const shareKey = access.kind === "member" && open ? `${apiBase}/share` : null;
  const { data: experimentShare, mutate: mutateExperimentShare } =
    useSWR<ExperimentShareInfo>(shareKey, fetcher, {
      revalidateOnFocus: false,
    });

  const revisionKey = open?.has_active_trials ? `${apiBase}/revision` : null;
  useSWR<ExperimentRevisionResponse>(revisionKey, fetcher, {
    refreshInterval: 5_000,
    revalidateOnFocus: false,
    onSuccess(next) {
      if (!open || next.revision === open.revision) return;
      void mutateOpenPages();
      void mutateTrialPages();
    },
  });

  const hasMoreTaskShells = Boolean(lastOpenPage?.next_cursor);
  const hasMoreTrials = Boolean(lastTrialPage?.next_cursor);
  const canLoadMore =
    (hasMoreTaskShells || hasMoreTrials) &&
    !isValidatingOpen &&
    !isValidatingTrials;
  const loadMoreRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    const target = loadMoreRef.current;
    if (!target || !canLoadMore) return;
    const observer = new IntersectionObserver((entries) => {
      if (!entries.some((entry) => entry.isIntersecting)) return;
      if (hasMoreTaskShells) void setOpenSize((size) => size + 1);
      if (hasMoreTrials) void setTrialSize((size) => size + 1);
    });
    observer.observe(target);
    return () => observer.disconnect();
  }, [
    canLoadMore,
    hasMoreTaskShells,
    hasMoreTrials,
    setOpenSize,
    setTrialSize,
  ]);

  const [isEditingName, setIsEditingName] = useState(false);
  const [nameDraft, setNameDraft] = useState("");
  const [nameError, setNameError] = useState<string | null>(null);
  const [isSavingName, setIsSavingName] = useState(false);
  const [copiedExperimentName, setCopiedExperimentName] = useState(false);
  const copiedTimeoutRef = useRef<number | null>(null);
  useEffect(
    () => () => {
      if (copiedTimeoutRef.current !== null) {
        window.clearTimeout(copiedTimeoutRef.current);
      }
    },
    []
  );

  const experimentId =
    access.kind === "member" ? access.experimentId : undefined;
  const displayName = open?.name || "Experiment";
  const probeHostTask = useMemo(
    () => resolveProbeHostTask(tasksForExperiment),
    [tasksForExperiment]
  );

  const refreshVisibleResources = useCallback(async () => {
    await Promise.all([
      mutateOpenPages(),
      mutateTrialPages(),
      mutateCostTotals(),
    ]);
  }, [mutateCostTotals, mutateOpenPages, mutateTrialPages]);

  async function handleRename() {
    if (access.kind !== "member") return;
    const nextName = nameDraft.trim();
    if (!nextName) {
      setNameError("Experiment name cannot be empty.");
      return;
    }
    setIsSavingName(true);
    setNameError(null);
    try {
      const response = await fetch(
        `/api/experiments/${encodeExperimentRouteParam(access.experimentId)}`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: nextName }),
        }
      );
      if (!response.ok) {
        const body = (await response.json().catch(() => ({}))) as {
          detail?: string;
          error?: string;
        };
        throw new Error(body.detail || body.error || "Rename failed");
      }
      await mutateOpenPages(
        (pages) =>
          pages?.map((page) => ({
            ...page,
            name: nextName,
            tasks: page.tasks.map((task) => ({
              ...task,
              experiment_name: nextName,
            })),
          })),
        { revalidate: false }
      );
      setIsEditingName(false);
      void refreshVisibleResources();
    } catch (error) {
      setNameError(error instanceof Error ? error.message : "Rename failed");
    } finally {
      setIsSavingName(false);
    }
  }

  async function handleUnlinkTask(task: Task) {
    if (access.kind !== "member") return;
    const response = await fetch(
      `${apiBase}/tasks/${encodeURIComponent(task.id)}`,
      { method: "DELETE" }
    );
    if (!response.ok) throw new Error("Failed to unlink task from experiment");
    await mutateOpenPages(
      (pages) =>
        pages?.map((page) => ({
          ...page,
          tasks: page.tasks.filter((item) => item.id !== task.id),
        })),
      { revalidate: false }
    );
    await mutateTrialPages(
      (pages) =>
        pages?.map((page) => ({
          ...page,
          tasks: page.tasks.filter((item) => item.id !== task.id),
        })),
      { revalidate: false }
    );
    void refreshVisibleResources();
  }

  async function handleDeleteTrial(trial: Trial) {
    if (access.kind !== "member") return;
    const response = await fetch(
      `/api/trials/${encodeURIComponent(trial.id)}`,
      {
        method: "DELETE",
      }
    );
    if (!response.ok) throw new Error("Failed to delete trial");
    await mutateTrialPages(
      (pages) =>
        pages?.map((page) => ({
          ...page,
          tasks: page.tasks.map((task) => ({
            ...task,
            trials: task.trials?.filter((item) => item.id !== trial.id),
          })),
        })),
      { revalidate: false }
    );
    void refreshVisibleResources();
  }

  async function handleCopyName() {
    await navigator.clipboard.writeText(displayName);
    setCopiedExperimentName(true);
    if (copiedTimeoutRef.current !== null) {
      window.clearTimeout(copiedTimeoutRef.current);
    }
    copiedTimeoutRef.current = window.setTimeout(() => {
      setCopiedExperimentName(false);
      copiedTimeoutRef.current = null;
    }, 2_000);
  }

  const isLoadingTrials = Boolean(
    open && !firstTrialPage && (isLoadingTrialPages || isValidatingTrials)
  );
  const loadedTrialCount = (trialResponses ?? []).reduce(
    (total, page) => total + page.trial_count,
    0
  );
  const trialsStalled = Boolean(trialError && !firstTrialPage);
  const hasFatalError = Boolean(openError && !open);
  const description = isPublic
    ? (open?.description ?? null)
    : experimentShare === undefined
      ? (open?.description ?? null)
      : experimentShare.description;
  const fullDescriptionUnavailable = Boolean(
    access.kind === "member" &&
    open?.description_truncated &&
    experimentShare === undefined
  );

  const headerLeft = isPublic ? (
    <h1 className="truncate pb-1 font-mono text-[26px] leading-[1.25] font-semibold tracking-[-0.02em] text-[color:var(--paper-ink)]">
      {displayName}
    </h1>
  ) : isEditingName ? (
    <div className="flex flex-wrap items-center gap-2">
      <Input
        value={nameDraft}
        onChange={(event) => setNameDraft(event.target.value)}
        className="h-10 w-[320px]"
        placeholder="Experiment name"
      />
      <Button
        size="sm"
        onClick={() => void handleRename()}
        disabled={isSavingName}
      >
        {isSavingName ? "Saving..." : "Save"}
      </Button>
      <Button
        variant="ghost"
        size="sm"
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
        onClick={() => void handleCopyName()}
        className="h-auto max-w-full min-w-0 justify-start truncate bg-transparent p-0 pb-1 font-mono text-[26px] leading-[1.25] font-semibold"
      >
        <h1 className="truncate">{displayName}</h1>
      </Button>
      {copiedExperimentName ? (
        <span aria-live="polite" className="text-[11px]">
          copied
        </span>
      ) : null}
      <Button
        type="button"
        variant="ghost"
        size="icon"
        onClick={() => {
          setNameDraft(displayName);
          setNameError(null);
          setIsEditingName(true);
        }}
        className="h-6 w-6"
        aria-label="Rename experiment"
      >
        <Pencil className="h-3.5 w-3.5" />
      </Button>
    </div>
  );

  return (
    <div className="space-y-4">
      <ExperimentDetailView
        experimentId={experimentId}
        tasksForExperiment={tasksForExperiment}
        costTotals={costTotals}
        costTotalsPending={costTotalsPending}
        exactSummary={open?.summary}
        isLoading={isLoadingOpen}
        isLoadingTrials={isLoadingTrials}
        hasError={hasFatalError}
        errorTitle="Failed to load experiment"
        errorDescription={
          isPublic
            ? "The share link may be invalid or no longer public."
            : "Check the API connection and try again."
        }
        headerLeft={headerLeft}
        headerStatus={
          isLoadingTrials || isValidatingTrials ? (
            <div className="text-muted-foreground flex items-center gap-1.5 text-[10px]">
              <Loader2 className="h-3 w-3 animate-spin" />
              <span>Loading trials {loadedTrialCount || ""}</span>
            </div>
          ) : experimentShare?.shadow_of && canManage ? (
            <Link
              href={`/experiments/${encodeExperimentRouteParam(experimentShare.shadow_of)}`}
              className="text-muted-foreground text-[10px] hover:underline"
            >
              ⇄ graded experiment
            </Link>
          ) : experimentShare?.qa_report_experiment_id && canManage ? (
            <Link
              href={`/experiments/${encodeExperimentRouteParam(experimentShare.qa_report_experiment_id)}`}
              className="text-muted-foreground text-[10px] hover:underline"
            >
              ⇄ QA report
            </Link>
          ) : null
        }
        headerRight={
          access.kind === "member" ? (
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
                experimentId={access.experimentId}
                canManageShare={canManage}
              />
            </div>
          ) : null
        }
        headerDescription={
          <ExperimentDescription
            experimentId={experimentId}
            description={description}
            readOnly={isPublic || fullDescriptionUnavailable}
            onSaved={
              access.kind === "member"
                ? (next) => {
                    void mutateExperimentShare(
                      (current) =>
                        current ? { ...current, description: next } : current,
                      { revalidate: false }
                    );
                    void mutateOpenPages();
                  }
                : undefined
            }
          />
        }
        inlineAlert={
          nameError ? (
            <Alert variant="destructive">
              <AlertTitle>Rename failed</AlertTitle>
              <AlertDescription>{nameError}</AlertDescription>
            </Alert>
          ) : trialsStalled ? (
            <Alert variant="destructive">
              <AlertTitle>Trial results failed to load</AlertTitle>
              <AlertDescription>
                <Button
                  size="sm"
                  onClick={() => void mutateTrialPages()}
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
        mode={access.kind}
        apiBaseUrl={apiBase}
        onTaskUnlink={access.kind === "member" ? handleUnlinkTask : undefined}
        onTrialDelete={
          access.kind === "member"
            ? (trial) => handleDeleteTrial(trial)
            : undefined
        }
        onRerun={
          access.kind === "member"
            ? () => void refreshVisibleResources()
            : undefined
        }
      />
      <div ref={loadMoreRef} className="h-px" aria-hidden="true" />
      {canLoadMore ? (
        <div className="flex justify-center">
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={() => {
              if (hasMoreTaskShells) void setOpenSize((size) => size + 1);
              if (hasMoreTrials) void setTrialSize((size) => size + 1);
            }}
          >
            Load more
          </Button>
        </div>
      ) : null}
    </div>
  );
}
