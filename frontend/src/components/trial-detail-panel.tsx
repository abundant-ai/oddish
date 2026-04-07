"use client";

import { useState, useEffect, useCallback, useMemo, useRef } from "react";
import { useSearchParams } from "next/navigation";
import {
  ResizableDrawer,
  DrawerHeader,
  DrawerTitle,
  DrawerDescription,
} from "@/components/ui/resizable-drawer";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  FileText,
  FolderOpen,
  AlertCircle,
  ChevronDown,
  ChevronUp,
  ChevronLeft,
  ChevronRight,
  Ban,
  RotateCcw,
  Loader2,
  Microscope,
  CheckCircle2,
  XCircle,
  AlertTriangle,
  Route,
  Package,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { TrajectoryViewer } from "@/components/trajectory-viewer";
import { TaskFilesPanel } from "@/components/task-files-panel";
import { TimingBreakdownBar } from "@/components/timing-breakdown-bar";
import { ArtifactsViewer } from "@/components/artifacts-viewer";
import type { Trial, Task } from "@/lib/types";
import {
  getMatrixStatus,
  STATUS_CONFIG,
  type MatrixStatus,
} from "@/lib/status-config";
import { HarborStageTimeline } from "@/components/harbor-stage-timeline";
import { HarborStageBadge } from "@/components/harbor-stage-badge";
import { QueueKeyIcon } from "@/components/queue-key-icon";

interface TrialDetailPanelProps {
  isOpen: boolean;
  onClose: () => void;
  trial: Trial | null;
  task: Task | null;
  orderedTrials?: Trial[] | null;
  trialIndex?: number | null;
  trialGroups?: Array<{
    agent: string;
    model: string | null;
    trials: Trial[];
  }> | null;
  onNavigate?: (trial: Trial, trialIndex: number) => void;
  onNavigateToTask?: () => void;
  onRetry?: (taskIds?: string[]) => void;
  apiBaseUrl?: string;
  allowRetry?: boolean;
  /** Render content only without ResizableDrawer wrapper */
  contentOnly?: boolean;
}

const OUTCOME_CARD_TONE: Record<MatrixStatus, string> = {
  pass: "border-emerald-500/30 bg-emerald-500/10",
  fail: "border-red-500/30 bg-red-500/10",
  "harness-error": "border-yellow-500/30 bg-yellow-500/10",
  pending: "border-gray-500/30 bg-gray-500/10",
  queued: "border-purple-500/30 bg-purple-500/10",
  running: "border-blue-500/30 bg-blue-500/10",
};

function getQueueSnapshotItems(trial: Trial): string[] {
  const queueInfo = trial.queue_info;
  if (!queueInfo) return [];

  return [
    queueInfo.position != null
      ? `Queue #${queueInfo.position} of ${queueInfo.queued_count}`
      : null,
    queueInfo.ahead != null ? `${queueInfo.ahead} ahead` : null,
    `${queueInfo.running_count} running`,
    `${queueInfo.concurrency_limit} slots`,
  ].filter((value): value is string => Boolean(value));
}

export function TrialDetailPanel({
  isOpen,
  onClose,
  trial,
  task,
  orderedTrials,
  trialIndex,
  trialGroups,
  onNavigate,
  onNavigateToTask,
  onRetry,
  apiBaseUrl = "/api",
  allowRetry = true,
  contentOnly = false,
}: TrialDetailPanelProps) {
  const searchParams = useSearchParams();

  const validTabs = useMemo(
    () => new Set(["summary", "files", "trajectory", "artifacts"]),
    [],
  );

  const [activeTab, setActiveTab] = useState(() => {
    const urlTab = searchParams.get("tab");
    return urlTab && validTabs.has(urlTab) ? urlTab : "summary";
  });
  const [showFullError, setShowFullError] = useState(false);
  const [retrying, setRetrying] = useState(false);
  const [retryError, setRetryError] = useState<string | null>(null);
  const [analysisRunning, setAnalysisRunning] = useState(false);
  const [analysisError, setAnalysisError] = useState<string | null>(null);
  const [filesTargetPath, setFilesTargetPath] = useState<string | null>(() =>
    searchParams.get("file"),
  );

  const hydratedFromUrl = useRef(false);

  // Hydrate from URL on first open
  useEffect(() => {
    if (!isOpen || hydratedFromUrl.current) return;
    hydratedFromUrl.current = true;
    const urlTab = searchParams.get("tab");
    const urlFile = searchParams.get("file");
    if (urlTab && validTabs.has(urlTab)) setActiveTab(urlTab);
    if (urlFile) {
      setFilesTargetPath(urlFile);
      if (!urlTab) setActiveTab("files");
    }
  }, [isOpen, searchParams, validTabs]);

  // Sync tab & file to URL (without triggering Next.js router navigation)
  useEffect(() => {
    if (!isOpen || !hydratedFromUrl.current) return;
    const next = new URLSearchParams(searchParams.toString());

    if (activeTab && activeTab !== "summary") {
      next.set("tab", activeTab);
    } else {
      next.delete("tab");
    }

    if (filesTargetPath) {
      next.set("file", filesTargetPath);
    } else {
      next.delete("file");
    }

    if (next.toString() !== searchParams.toString()) {
      const url = `${window.location.pathname}${next.toString() ? `?${next.toString()}` : ""}`;
      window.history.replaceState(window.history.state, "", url);
    }
  }, [isOpen, activeTab, filesTargetPath, searchParams]);

  const canRetry =
    allowRetry && (trial?.status === "failed" || trial?.status === "success");
  const taskHasActiveTrials =
    task !== null
      ? Math.max(0, task.total - task.completed - task.failed) > 0
      : false;
  const canRunAnalysis =
    allowRetry &&
    !taskHasActiveTrials &&
    (task?.run_analysis ||
      trial?.analysis_status != null ||
      trial?.analysis != null);
  const analysisLabel =
    trial?.analysis_status || trial?.analysis
      ? "Rerun analysis"
      : "Run analysis";

  const handleRetry = async () => {
    if (!trial || retrying || !allowRetry) return;
    setRetrying(true);
    setRetryError(null);

    try {
      const res = await fetch(`${apiBaseUrl}/trials/${trial.id}/retry`, {
        method: "POST",
      });

      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || data.error || "Failed to retry trial");
      }

      onRetry?.(task ? [task.id] : undefined);
      onClose();
    } catch (err) {
      setRetryError(err instanceof Error ? err.message : "Failed to retry");
    } finally {
      setRetrying(false);
    }
  };

  const handleRunAnalysis = async () => {
    if (!trial || !task || analysisRunning || !canRunAnalysis) return;
    setAnalysisRunning(true);
    setAnalysisError(null);

    try {
      const res = await fetch(
        `${apiBaseUrl}/trials/${trial.id}/analysis/retry`,
        {
          method: "POST",
        },
      );

      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(
          data.detail || data.error || "Failed to queue analysis",
        );
      }

      onRetry?.([task.id]);
      onClose();
    } catch (err) {
      setAnalysisError(
        err instanceof Error ? err.message : "Failed to queue analysis",
      );
    } finally {
      setAnalysisRunning(false);
    }
  };

  const STAGE_FILE_MAP: Record<string, string> = {
    starting: "agent/oracle.txt",
    trial_started: "agent/oracle.txt",
    environment_setup: "agent/setup/stdout.txt",
    agent_running: "agent",
    verification: "verifier/test-stdout.txt",
    completed: "verifier/test-stdout.txt",
  };

  const handleTimelineStageClick = (stageId: string) => {
    const filePath = STAGE_FILE_MAP[stageId] ?? null;
    setActiveTab("files");
    setFilesTargetPath(filePath);
  };

  // Reset state when panel closes
  useEffect(() => {
    if (!isOpen) {
      setActiveTab("summary");
      setShowFullError(false);
      setRetrying(false);
      setRetryError(null);
      setAnalysisRunning(false);
      setAnalysisError(null);
      setFilesTargetPath(null);
      hydratedFromUrl.current = false;
    }
  }, [isOpen]);

  const orderedList = useMemo(
    () => orderedTrials ?? task?.trials ?? [],
    [orderedTrials, task?.trials],
  );
  const resolvedIndex =
    typeof trialIndex === "number" && trialIndex >= 0
      ? trialIndex
      : trial
        ? orderedList.findIndex((item) => item.id === trial.id)
        : -1;
  const hasNavigation = orderedList.length > 1 && resolvedIndex >= 0;
  // Can navigate to task if at first trial and callback exists
  const canGoToTask = onNavigateToTask && resolvedIndex === 0;
  const canGoPrev = hasNavigation && resolvedIndex > 0;
  const canGoNext = hasNavigation && resolvedIndex < orderedList.length - 1;

  const isEditableTarget = (target: EventTarget | null) => {
    if (!target || !(target instanceof HTMLElement)) return false;
    const tag = target.tagName.toLowerCase();
    return (
      tag === "input" ||
      tag === "textarea" ||
      target.isContentEditable ||
      target.getAttribute("role") === "textbox"
    );
  };

  const navigateTo = useCallback(
    (nextIndex: number) => {
      if (!onNavigate) return;
      const nextTrial = orderedList[nextIndex];
      if (!nextTrial) return;
      onNavigate(nextTrial, nextIndex);
    },
    [onNavigate, orderedList],
  );

  useEffect(() => {
    if (!isOpen || !hasNavigation || !onNavigate) return;

    const handleKeyDown = (event: KeyboardEvent) => {
      if (isEditableTarget(event.target)) return;
      if (event.key === "ArrowLeft") {
        event.preventDefault();
        if (canGoPrev) {
          navigateTo(resolvedIndex - 1);
        } else if (canGoToTask) {
          onNavigateToTask?.();
        }
      } else if (event.key === "ArrowRight" && canGoNext) {
        event.preventDefault();
        navigateTo(resolvedIndex + 1);
      }
    };

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [
    isOpen,
    hasNavigation,
    onNavigate,
    onNavigateToTask,
    canGoPrev,
    canGoNext,
    canGoToTask,
    resolvedIndex,
    navigateTo,
  ]);

  if (!trial || !task) {
    return null;
  }
  const trialStatus = getMatrixStatus(
    trial.status,
    trial.reward,
    trial.error_message,
  );
  const trialStatusConfig = STATUS_CONFIG[trialStatus];
  const TrialStatusIcon = trialStatusConfig.icon;

  const resolvedGroups =
    trialGroups && trialGroups.length > 0
      ? trialGroups
      : [
          {
            agent: trial.agent,
            model: trial.model ?? null,
            trials: orderedList,
          },
        ];
  const currentGroupIndex = resolvedGroups.findIndex((group) =>
    group.trials.some((groupTrial) => groupTrial.id === trial.id),
  );
  const currentGroup =
    currentGroupIndex >= 0 ? resolvedGroups[currentGroupIndex] : null;
  const currentGroupTrials = currentGroup?.trials ?? [];
  const currentGroupTrialIndex = currentGroupTrials.findIndex(
    (groupTrial) => groupTrial.id === trial.id,
  );

  const navigateToGroupTrial = (groupIndex: number) => {
    if (!onNavigate || !currentGroup) return;
    const nextTrial = currentGroup.trials[groupIndex];
    if (!nextTrial) return;
    const nextIndex = orderedList.findIndex((item) => item.id === nextTrial.id);
    if (nextIndex < 0) return;
    onNavigate(nextTrial, nextIndex);
  };

  const content = (
    <>
      <DrawerHeader className="border-b border-border px-4 py-3 sm:px-6 sm:py-4">
        <DrawerTitle className="flex min-w-0 items-center gap-2 pr-8 font-mono text-sm sm:text-base">
          <span className="min-w-0 truncate">{trial.name}</span>
          {trial.task_version != null && (
            <span className="inline-flex shrink-0 items-center rounded-md border border-border bg-muted/50 px-1.5 py-0.5 font-mono text-[11px] font-medium text-muted-foreground">
              v{trial.task_version}
            </span>
          )}
          <span className="text-muted-foreground/50">·</span>
          <span className="flex min-w-0 flex-col items-center text-center leading-tight text-muted-foreground">
            <span className="truncate text-[10px] font-bold sm:text-xs">
              {trial.agent}
            </span>
            <span className="flex items-center gap-1 truncate font-mono text-[9px] font-normal sm:text-[10px]">
              <QueueKeyIcon
                queueKey={trial.provider}
                model={trial.model}
                agent={trial.agent}
                size={11}
                className="shrink-0"
              />
              {trial.model ?? "—"}
            </span>
          </span>
        </DrawerTitle>
        <DrawerDescription className="font-mono text-muted-foreground">
          <span className="truncate">{trial.id}</span>
        </DrawerDescription>
        <div className="flex flex-wrap items-stretch justify-between gap-2 pt-2 text-xs text-muted-foreground">
          <div className="flex items-center gap-1">
            {hasNavigation && (
              <>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  onClick={() => {
                    if (canGoPrev) {
                      navigateTo(resolvedIndex - 1);
                    } else if (canGoToTask) {
                      onNavigateToTask?.();
                    }
                  }}
                  disabled={!canGoPrev && !canGoToTask}
                  className="h-7 w-7"
                  aria-label={
                    canGoPrev
                      ? "Previous trial"
                      : canGoToTask
                        ? "View task"
                        : "Previous"
                  }
                  title={
                    canGoPrev
                      ? "Previous trial"
                      : canGoToTask
                        ? "View task"
                        : "Previous"
                  }
                >
                  <ChevronLeft className="h-4 w-4" />
                </Button>

                {currentGroupTrials.map((groupTrial, index) => {
                  const groupStatus = getMatrixStatus(
                    groupTrial.status,
                    groupTrial.reward,
                    groupTrial.error_message,
                  );
                  const groupConfig = STATUS_CONFIG[groupStatus];
                  const isActive = index === currentGroupTrialIndex;
                  return (
                    <Button
                      key={groupTrial.id}
                      type="button"
                      variant="ghost"
                      size="icon"
                      onClick={() => navigateToGroupTrial(index)}
                      className={cn(
                        "flex h-5 w-5 items-center justify-center rounded-sm border p-0 text-sm font-semibold leading-none transition",
                        groupConfig.matrixClass,
                        isActive
                          ? "ring-2 ring-primary/60 ring-offset-1 ring-offset-background"
                          : "",
                      )}
                      aria-label={`Trial ${index + 1}`}
                      title={`${groupConfig.shortLabel} • Trial ${index + 1}`}
                    >
                      {groupStatus === "pending" ||
                      groupStatus === "queued" ||
                      groupStatus === "running" ? (
                        <Loader2 className="h-3.5 w-3.5" />
                      ) : groupStatus === "harness-error" ? (
                        <Ban className="h-3.5 w-3.5" />
                      ) : (
                        groupConfig.symbol
                      )}
                    </Button>
                  );
                })}
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  onClick={() => navigateTo(resolvedIndex + 1)}
                  disabled={!canGoNext}
                  className="h-7 w-7"
                  aria-label="Next trial"
                  title="Next trial"
                >
                  <ChevronRight className="h-4 w-4" />
                </Button>
              </>
            )}
          </div>
          <div className="flex min-w-0 items-stretch gap-2">
            <Card
              className={cn(
                "min-w-[145px] border",
                OUTCOME_CARD_TONE[trialStatus],
              )}
            >
              <CardContent className="px-2 py-1">
                <div className="flex items-center gap-1.5">
                  <TrialStatusIcon
                    className={cn(
                      "h-3.5 w-3.5 shrink-0",
                      trialStatus === "pass"
                        ? "text-emerald-500"
                        : trialStatus === "fail"
                          ? "text-red-500"
                          : trialStatus === "harness-error"
                            ? "text-yellow-500"
                            : trialStatus === "queued"
                              ? "text-purple-500"
                              : trialStatus === "running"
                                ? "text-blue-500"
                                : "text-gray-500",
                      (trialStatus === "pending" ||
                        trialStatus === "queued" ||
                        trialStatus === "running") &&
                        "animate-spin",
                    )}
                  />
                  <div className="min-w-0">
                    <div className="text-[8px] uppercase leading-none tracking-wider text-muted-foreground">
                      Reward
                    </div>
                    <div className="flex items-baseline gap-1">
                      <span className="font-mono text-sm font-bold leading-none">
                        {trial.reward !== null ? trial.reward : "—"}
                      </span>
                      <span className="text-[9px] capitalize leading-none text-muted-foreground">
                        {trialStatusConfig.shortLabel}
                      </span>
                    </div>
                  </div>
                </div>
              </CardContent>
            </Card>
            {canRetry && (
              <Button
                onClick={handleRetry}
                disabled={retrying}
                variant="outline"
                size="sm"
                className="h-7 min-w-[128px] px-2 text-[10px] font-semibold uppercase tracking-wide"
              >
                {retrying ? (
                  <>
                    <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />
                    Retrying...
                  </>
                ) : (
                  <>
                    <RotateCcw className="mr-1 h-3.5 w-3.5" />
                    Retry Trial
                  </>
                )}
              </Button>
            )}
            {canRunAnalysis && (
              <Button
                onClick={handleRunAnalysis}
                disabled={analysisRunning}
                variant="outline"
                size="sm"
                className="h-7 min-w-[148px] px-2 text-[10px] font-semibold uppercase tracking-wide"
              >
                {analysisRunning ? (
                  <>
                    <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />
                    Queueing...
                  </>
                ) : (
                  <>
                    <Microscope className="mr-1 h-3.5 w-3.5" />
                    {analysisLabel}
                  </>
                )}
              </Button>
            )}
          </div>
        </div>
        {retryError && (
          <p className="pt-1 text-right text-xs text-red-500">{retryError}</p>
        )}
        {analysisError && (
          <p className="pt-1 text-right text-xs text-red-500">
            {analysisError}
          </p>
        )}
      </DrawerHeader>

      <Tabs
        value={activeTab}
        onValueChange={setActiveTab}
        className="flex flex-1 flex-col overflow-hidden"
      >
        <div className="border-b border-border px-4 sm:px-6">
          <TabsList className="h-10 gap-0 border-0 bg-transparent p-0 sm:h-12">
            <TabsTrigger
              value="summary"
              className="rounded-none px-3 text-xs data-[state=active]:border-b-2 data-[state=active]:border-primary data-[state=active]:bg-transparent sm:px-4 sm:text-sm"
            >
              <FileText className="mr-1 h-3.5 w-3.5 sm:mr-2 sm:h-4 sm:w-4" />
              Summary
            </TabsTrigger>
            <TabsTrigger
              value="files"
              className="rounded-none px-3 text-xs data-[state=active]:border-b-2 data-[state=active]:border-primary data-[state=active]:bg-transparent sm:px-4 sm:text-sm"
            >
              <FolderOpen className="mr-1 h-3.5 w-3.5 sm:mr-2 sm:h-4 sm:w-4" />
              Files
            </TabsTrigger>
            <TabsTrigger
              value="trajectory"
              className="rounded-none px-3 text-xs data-[state=active]:border-b-2 data-[state=active]:border-primary data-[state=active]:bg-transparent sm:px-4 sm:text-sm"
            >
              <Route className="mr-1 h-3.5 w-3.5 sm:mr-2 sm:h-4 sm:w-4" />
              Trajectory
            </TabsTrigger>
            <TabsTrigger
              value="artifacts"
              className="rounded-none px-3 text-xs data-[state=active]:border-b-2 data-[state=active]:border-primary data-[state=active]:bg-transparent sm:px-4 sm:text-sm"
            >
              <Package className="mr-1 h-3.5 w-3.5 sm:mr-2 sm:h-4 sm:w-4" />
              Artifacts
            </TabsTrigger>
          </TabsList>
        </div>

        <div className="flex-1 overflow-auto">
          <TabsContent value="summary" className="m-0 p-4 sm:p-6">
            <div className="space-y-4 pb-4">
              {trial.queue_info && (
                <Card className="border-purple-500/30 bg-purple-500/5">
                  <CardHeader className="px-4 pb-1 pt-2">
                    <CardTitle className="text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
                      Queue Snapshot
                    </CardTitle>
                  </CardHeader>
                  <CardContent className="px-4 pb-3">
                    <div className="flex flex-wrap gap-2">
                      {getQueueSnapshotItems(trial).map((item) => (
                        <span
                          key={item}
                          className="rounded border border-purple-500/20 bg-background/60 px-2 py-1 font-mono text-[11px] text-foreground"
                        >
                          {item}
                        </span>
                      ))}
                    </div>
                    <p className="mt-2 text-xs text-muted-foreground">
                      Live scheduler snapshot. This can move as other trials
                      start, finish, or get retried.
                    </p>
                  </CardContent>
                </Card>
              )}
              {/* Analysis Card - only show if analysis is enabled/running/complete */}
              {(trial.analysis_status || trial.analysis) && (
                <Card
                  className={
                    trial.analysis_status === "running" ||
                    trial.analysis_status === "pending" ||
                    trial.analysis_status === "queued"
                      ? "border-blue-500/30 bg-blue-500/5"
                      : trial.analysis?.classification?.startsWith("GOOD")
                        ? "border-emerald-500/30 bg-emerald-500/5"
                        : trial.analysis?.classification?.startsWith("BAD")
                          ? "border-amber-500/30 bg-amber-500/5"
                          : "border-slate-500/30 bg-slate-500/5"
                  }
                >
                  <CardContent className="px-4 py-3">
                    <div className="flex items-start gap-3">
                      {trial.analysis_status === "running" ||
                      trial.analysis_status === "pending" ||
                      trial.analysis_status === "queued" ? (
                        <Microscope className="mt-0.5 h-5 w-5 animate-pulse text-blue-500" />
                      ) : trial.analysis?.classification?.startsWith("GOOD") ? (
                        <CheckCircle2 className="mt-0.5 h-5 w-5 text-emerald-500" />
                      ) : trial.analysis?.classification?.startsWith("BAD") ? (
                        <AlertTriangle className="mt-0.5 h-5 w-5 text-amber-500" />
                      ) : (
                        <XCircle className="mt-0.5 h-5 w-5 text-slate-500" />
                      )}
                      <div className="min-w-0 flex-1">
                        <div className="flex flex-col gap-1">
                          <span className="font-mono text-sm font-bold">
                            {trial.analysis_status === "running" ||
                            trial.analysis_status === "pending" ||
                            trial.analysis_status === "queued"
                              ? "Analyzing..."
                              : trial.analysis?.classification?.replace(
                                  "_",
                                  " ",
                                ) || "Analysis"}
                          </span>
                          {trial.analysis?.subtype && (
                            <span className="text-xs text-muted-foreground">
                              Reason: {trial.analysis.subtype}
                            </span>
                          )}
                        </div>
                        {trial.analysis?.evidence && (
                          <p className="mt-2 text-xs leading-relaxed text-muted-foreground/90">
                            {trial.analysis.evidence}
                          </p>
                        )}
                        {trial.analysis?.root_cause &&
                          trial.analysis.root_cause !==
                            trial.analysis.evidence && (
                            <p className="mt-1 text-xs text-muted-foreground">
                              {trial.analysis.root_cause}
                            </p>
                          )}
                        {trial.analysis?.recommendation &&
                          trial.analysis.recommendation !== "N/A" && (
                            <p className="mt-1 text-xs italic text-muted-foreground/80">
                              💡 {trial.analysis.recommendation}
                            </p>
                          )}
                      </div>
                    </div>
                  </CardContent>
                </Card>
              )}

              {/* Execution Timeline - shows progress during running trials */}
              {trial.harbor_stage && (
                <Card>
                  <CardHeader className="px-4 pb-1 pt-2">
                    <CardTitle className="flex items-center justify-between text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
                      <span>Execution Timeline</span>
                      <HarborStageBadge stage={trial.harbor_stage} />
                    </CardTitle>
                  </CardHeader>
                  <CardContent className="px-4 pb-2">
                    <HarborStageTimeline
                      currentStage={trial.harbor_stage}
                      status={trial.status}
                      isFailure={
                        trial.status === "failed" ||
                        Boolean(trial.error_message)
                      }
                      onStageClick={handleTimelineStageClick}
                      phaseTiming={trial.phase_timing}
                      startedAt={trial.started_at}
                      finishedAt={trial.finished_at}
                    />
                  </CardContent>
                </Card>
              )}

              <TimingBreakdownBar
                createdAt={trial.created_at}
                startedAt={trial.started_at}
                finishedAt={trial.finished_at}
                compact
              />

              {/* Error Card */}
              {trial.error_message && (
                <Card className="border-red-500/30 bg-red-500/5">
                  <CardContent className="px-4 py-3">
                    <div className="flex items-start gap-2">
                      <AlertCircle className="mt-0.5 h-4 w-4 shrink-0 text-red-500" />
                      <div className="min-w-0 flex-1">
                        <pre className="whitespace-pre-wrap break-words font-mono text-sm text-red-600 dark:text-red-400">
                          {showFullError
                            ? trial.error_message
                            : trial.error_message.slice(0, 300)}
                          {trial.error_message.length > 300 &&
                            !showFullError &&
                            "..."}
                        </pre>
                        {trial.error_message.length > 300 && (
                          <Button
                            type="button"
                            variant="ghost"
                            size="sm"
                            onClick={() => setShowFullError(!showFullError)}
                            className="mt-2 h-auto px-0 text-xs text-red-500/60 hover:text-red-600"
                          >
                            {showFullError ? (
                              <>
                                <ChevronUp className="h-3 w-3" />
                                Show less
                              </>
                            ) : (
                              <>
                                <ChevronDown className="h-3 w-3" />
                                Show full error
                              </>
                            )}
                          </Button>
                        )}
                      </div>
                    </div>
                  </CardContent>
                </Card>
              )}
            </div>
          </TabsContent>

          <TabsContent value="files" className="m-0 h-full p-0">
            <TaskFilesPanel
              isOpen={isOpen && activeTab === "files"}
              onClose={() => {}}
              taskId={null}
              filesUrl={`${apiBaseUrl}/trials/${trial.id}/files`}
              initialFilePath={filesTargetPath}
              contentOnly
            />
          </TabsContent>

          <TabsContent
            value="artifacts"
            className="m-0 h-full overflow-auto p-0"
          >
            <ArtifactsViewer
              filesUrl={`${apiBaseUrl}/trials/${trial.id}/files`}
            />
          </TabsContent>

          <TabsContent
            value="trajectory"
            className="m-0 h-full overflow-auto p-0"
          >
            <TrajectoryViewer trialId={trial.id} apiBaseUrl={apiBaseUrl} />
          </TabsContent>
        </div>
      </Tabs>
    </>
  );

  if (contentOnly) {
    return (
      <div className="flex h-full flex-1 flex-col overflow-hidden">
        {content}
      </div>
    );
  }

  return (
    <ResizableDrawer
      open={isOpen}
      onOpenChange={(open) => !open && onClose()}
      defaultWidth={700}
      minWidth={420}
      maxWidth={900}
    >
      {content}
    </ResizableDrawer>
  );
}
