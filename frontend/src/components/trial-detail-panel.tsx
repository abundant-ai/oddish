"use client";

import { useState, useEffect, useCallback, useMemo } from "react";
import useSWR from "swr";
import { mutate } from "swr";
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
  Terminal,
  Bot,
  FlaskConical,
  Flame,
  Package,
} from "lucide-react";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";
import { TrajectoryViewer } from "@/components/trajectory-viewer";
import { TaskFilesPanel } from "@/components/task-files-panel";
import { CodeBlock } from "@/components/code-block";
import { TimingBreakdownBar } from "@/components/timing-breakdown-bar";
import { ArtifactsViewer } from "@/components/artifacts-viewer";
import type { Trial, Task } from "@/lib/types";
import {
  getMatrixStatus,
  STATUS_CONFIG,
  type MatrixStatus,
} from "@/lib/status-config";
import { fetcher } from "@/lib/api";
import { HarborStageTimeline } from "@/components/harbor-stage-timeline";
import { HarborStageBadge } from "@/components/harbor-stage-badge";
import { QueueKeyIcon } from "@/components/queue-key-icon";

interface StructuredLogs {
  trial_id: string;
  agent: {
    oracle: string | null;
    setup: string | null;
    commands: Array<{ name: string; content: string }>;
  };
  verifier: {
    stdout: string | null;
    stderr: string | null;
  };
  other: Array<{ name: string; content: string }>;
  exception: string | null;
}

type LogCategory = "agent" | "verifier" | "other" | "exception";

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
  onRetry?: () => void;
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
  const [activeTab, setActiveTab] = useState("summary");
  const [showFullError, setShowFullError] = useState(false);
  const [retrying, setRetrying] = useState(false);
  const [retryError, setRetryError] = useState<string | null>(null);
  const [logCategory, setLogCategory] = useState<LogCategory>("agent");
  const [logCategoryInitialized, setLogCategoryInitialized] = useState(false);
  const trialId = trial?.id;

  // Fetch structured logs when Logs tab is active
  const logsSwrKey =
    isOpen && trialId && activeTab === "logs"
      ? `${apiBaseUrl}/trials/${trialId}/logs/structured`
      : null;

  const {
    data: structuredLogs,
    error: logsSwrError,
    isLoading: logsLoading,
  } = useSWR<StructuredLogs>(logsSwrKey, fetcher, {
    revalidateOnFocus: false,
    revalidateOnReconnect: false,
  });

  // Auto-select first available log category
  useEffect(() => {
    if (!structuredLogs || logCategoryInitialized) return;

    if (
      structuredLogs.agent.oracle ||
      structuredLogs.agent.setup ||
      structuredLogs.agent.commands.length > 0
    ) {
      setLogCategory("agent");
    } else if (
      structuredLogs.verifier.stdout ||
      structuredLogs.verifier.stderr
    ) {
      setLogCategory("verifier");
    } else if (structuredLogs.other && structuredLogs.other.length > 0) {
      setLogCategory("other");
    } else if (structuredLogs.exception) {
      setLogCategory("exception");
    }
    setLogCategoryInitialized(true);
  }, [structuredLogs, logCategoryInitialized]);

  // Prefetch trajectory while viewing summary to reduce perceived latency.
  useEffect(() => {
    if (!isOpen || !trialId) return;

    const trajectoryUrl = `${apiBaseUrl}/trials/${trialId}/trajectory`;

    if (activeTab !== "trajectory") {
      void mutate(trajectoryUrl, fetcher(trajectoryUrl), { revalidate: false });
    }
  }, [isOpen, trialId, apiBaseUrl, activeTab]);

  const canRetry =
    allowRetry && (trial?.status === "failed" || trial?.status === "success");

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

      // Success - trigger data refresh and close panel
      onRetry?.();
      onClose();
    } catch (err) {
      setRetryError(err instanceof Error ? err.message : "Failed to retry");
    } finally {
      setRetrying(false);
    }
  };

  // Reset state when panel closes
  useEffect(() => {
    if (!isOpen) {
      setActiveTab("summary");
      setShowFullError(false);
      setRetrying(false);
      setRetryError(null);
      setLogCategoryInitialized(false);
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

  const formatTime = (value?: string | null) => {
    if (!value) return "—";
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return "—";
    return parsed.toLocaleTimeString([], {
      hour: "numeric",
      minute: "2-digit",
    });
  };

  const formatDate = (value?: string | null) => {
    if (!value) return "—";
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return "—";
    return parsed.toLocaleDateString([], {
      month: "short",
      day: "numeric",
      year: "numeric",
    });
  };

  const formatDuration = (start?: string | null, end?: string | null) => {
    if (!start || !end) return "—";
    const startDate = new Date(start);
    const endDate = new Date(end);
    const diffMs = endDate.getTime() - startDate.getTime();
    if (diffMs < 0 || Number.isNaN(diffMs)) return "—";
    const seconds = Math.floor(diffMs / 1000);
    const minutes = Math.floor(seconds / 60);
    const hours = Math.floor(minutes / 60);
    if (hours > 0) {
      return `${hours}h ${minutes % 60}m ${seconds % 60}s`;
    }
    if (minutes > 0) {
      return `${minutes}m ${seconds % 60}s`;
    }
    return `${seconds}s`;
  };

  const content = (
    <>
      <DrawerHeader className="px-4 sm:px-6 py-3 sm:py-4 border-b border-border">
        <DrawerTitle className="flex items-center gap-2 font-mono text-sm sm:text-base pr-8 min-w-0">
          <span className="truncate min-w-0">{trial.name}</span>
          <span className="text-muted-foreground/50">·</span>
          <span className="flex flex-col items-center leading-tight text-center text-muted-foreground min-w-0">
            <span className="text-[10px] sm:text-xs font-bold truncate">
              {trial.agent}
            </span>
            <span className="text-[9px] sm:text-[10px] font-normal truncate flex items-center gap-1">
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
        <DrawerDescription className="flex items-center gap-1.5 font-mono text-muted-foreground">
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
                        "h-5 w-5 rounded-sm border p-0 text-sm font-semibold leading-none transition flex items-center justify-center",
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
          <div className="flex items-stretch gap-2 min-w-0">
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
                    <div className="text-[8px] uppercase tracking-wider text-muted-foreground leading-none">
                      Reward
                    </div>
                    <div className="flex items-baseline gap-1">
                      <span className="text-sm font-bold font-mono leading-none">
                        {trial.reward !== null ? trial.reward : "—"}
                      </span>
                      <span className="text-[9px] text-muted-foreground capitalize leading-none">
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
                    <Loader2 className="h-3.5 w-3.5 mr-1 animate-spin" />
                    Retrying...
                  </>
                ) : (
                  <>
                    <RotateCcw className="h-3.5 w-3.5 mr-1" />
                    Retry Trial
                  </>
                )}
              </Button>
            )}
          </div>
        </div>
        {retryError && (
          <p className="text-xs text-red-500 text-right pt-1">{retryError}</p>
        )}
      </DrawerHeader>

      <Tabs
        value={activeTab}
        onValueChange={setActiveTab}
        className="flex-1 flex flex-col overflow-hidden"
      >
        <div className="border-b border-border px-4 sm:px-6">
          <TabsList className="h-10 sm:h-12 bg-transparent border-0 p-0 gap-0">
            <TabsTrigger
              value="summary"
              className="data-[state=active]:bg-transparent data-[state=active]:border-b-2 data-[state=active]:border-primary rounded-none px-3 sm:px-4 text-xs sm:text-sm"
            >
              <FileText className="h-3.5 w-3.5 sm:h-4 sm:w-4 mr-1 sm:mr-2" />
              Summary
            </TabsTrigger>
            <TabsTrigger
              value="logs"
              className="data-[state=active]:bg-transparent data-[state=active]:border-b-2 data-[state=active]:border-primary rounded-none px-3 sm:px-4 text-xs sm:text-sm"
            >
              <Terminal className="h-3.5 w-3.5 sm:h-4 sm:w-4 mr-1 sm:mr-2" />
              Logs
            </TabsTrigger>
            <TabsTrigger
              value="files"
              className="data-[state=active]:bg-transparent data-[state=active]:border-b-2 data-[state=active]:border-primary rounded-none px-3 sm:px-4 text-xs sm:text-sm"
            >
              <FolderOpen className="h-3.5 w-3.5 sm:h-4 sm:w-4 mr-1 sm:mr-2" />
              Files
            </TabsTrigger>
            <TabsTrigger
              value="trajectory"
              className="data-[state=active]:bg-transparent data-[state=active]:border-b-2 data-[state=active]:border-primary rounded-none px-3 sm:px-4 text-xs sm:text-sm"
            >
              <Route className="h-3.5 w-3.5 sm:h-4 sm:w-4 mr-1 sm:mr-2" />
              Trajectory
            </TabsTrigger>
            <TabsTrigger
              value="artifacts"
              className="data-[state=active]:bg-transparent data-[state=active]:border-b-2 data-[state=active]:border-primary rounded-none px-3 sm:px-4 text-xs sm:text-sm"
            >
              <Package className="h-3.5 w-3.5 sm:h-4 sm:w-4 mr-1 sm:mr-2" />
              Artifacts
            </TabsTrigger>
          </TabsList>
        </div>

        <div className="flex-1 overflow-auto">
          <TabsContent value="summary" className="m-0 p-4 sm:p-6">
            <div className="space-y-4 pb-4">
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
                  <CardContent className="py-3 px-4">
                    <div className="flex items-start gap-3">
                      {trial.analysis_status === "running" ||
                      trial.analysis_status === "pending" ||
                      trial.analysis_status === "queued" ? (
                        <Microscope className="h-5 w-5 text-blue-500 animate-pulse mt-0.5" />
                      ) : trial.analysis?.classification?.startsWith("GOOD") ? (
                        <CheckCircle2 className="h-5 w-5 text-emerald-500 mt-0.5" />
                      ) : trial.analysis?.classification?.startsWith("BAD") ? (
                        <AlertTriangle className="h-5 w-5 text-amber-500 mt-0.5" />
                      ) : (
                        <XCircle className="h-5 w-5 text-slate-500 mt-0.5" />
                      )}
                      <div className="flex-1 min-w-0">
                        <div className="flex flex-col gap-1">
                          <span className="font-bold font-mono text-sm">
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
                        {trial.analysis?.root_cause && (
                          <p className="text-xs text-muted-foreground mt-1">
                            {trial.analysis.root_cause}
                          </p>
                        )}
                        {trial.analysis?.recommendation &&
                          trial.analysis.recommendation !== "N/A" && (
                            <p className="text-xs text-muted-foreground/80 mt-1 italic">
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
                  <CardHeader className="pb-1 pt-2 px-4">
                    <CardTitle className="text-[11px] font-semibold text-muted-foreground uppercase tracking-wider flex items-center justify-between">
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
                    />
                  </CardContent>
                </Card>
              )}

              <Card>
                <CardHeader className="pb-1 pt-2 px-4">
                  <CardTitle className="text-[11px] font-semibold text-muted-foreground uppercase tracking-wider">
                    Timing
                  </CardTitle>
                </CardHeader>
                <CardContent className="px-4 pb-3">
                  <TimingBreakdownBar
                    createdAt={trial.created_at}
                    startedAt={trial.started_at}
                    finishedAt={trial.finished_at}
                  />
                  <div className="text-[10px] text-muted-foreground uppercase tracking-wider mb-2 mt-3">
                    {formatDate(trial.created_at)}
                  </div>
                  <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 text-xs">
                    <div>
                      <span className="text-muted-foreground block">
                        Created
                      </span>
                      <span className="font-mono">
                        {formatTime(trial.created_at)}
                      </span>
                    </div>
                    <div>
                      <span className="text-muted-foreground block">
                        Started
                      </span>
                      <span className="font-mono">
                        {formatTime(trial.started_at)}
                      </span>
                    </div>
                    <div>
                      <span className="text-muted-foreground block">
                        Finished
                      </span>
                      <span className="font-mono">
                        {formatTime(trial.finished_at)}
                      </span>
                    </div>
                    <div>
                      <span className="text-muted-foreground block">
                        Duration
                      </span>
                      <span className="font-mono">
                        {formatDuration(trial.started_at, trial.finished_at)}
                      </span>
                    </div>
                  </div>
                </CardContent>
              </Card>

              {/* Error Card */}
              {trial.error_message && (
                <Card className="border-red-500/30 bg-red-500/5">
                  <CardContent className="py-3 px-4">
                    <div className="flex items-start gap-2">
                      <AlertCircle className="h-4 w-4 text-red-500 mt-0.5 shrink-0" />
                      <div className="min-w-0 flex-1">
                        <pre className="text-sm font-mono text-red-600 dark:text-red-400 whitespace-pre-wrap break-words">
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

          <TabsContent value="logs" className="m-0 h-full p-0">
            <div className="h-full flex">
              {logsLoading ? (
                <div className="flex-1 p-4 space-y-2">
                  <Skeleton className="h-4 w-full" />
                  <Skeleton className="h-4 w-3/4" />
                  <Skeleton className="h-4 w-5/6" />
                </div>
              ) : logsSwrError ? (
                <div className="flex-1 p-4 text-center">
                  <AlertCircle className="h-8 w-8 text-red-500 mx-auto mb-2" />
                  <p className="text-sm text-muted-foreground">
                    {logsSwrError?.message ?? "Failed to load logs"}
                  </p>
                </div>
              ) : (
                <>
                  <div className="w-28 sm:w-32 shrink-0 border-r border-border bg-muted/20 flex flex-col p-2 space-y-1">
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      onClick={() => setLogCategory("agent")}
                      className={cn(
                        "w-full justify-start gap-2 px-2 py-1.5 text-xs",
                        logCategory === "agent"
                          ? "bg-primary/10 text-primary font-medium"
                          : "text-muted-foreground hover:bg-muted",
                      )}
                    >
                      <Bot className="h-3.5 w-3.5" />
                      Agent
                    </Button>
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      onClick={() => setLogCategory("verifier")}
                      className={cn(
                        "w-full justify-start gap-2 px-2 py-1.5 text-xs",
                        logCategory === "verifier"
                          ? "bg-primary/10 text-primary font-medium"
                          : "text-muted-foreground hover:bg-muted",
                      )}
                    >
                      <FlaskConical className="h-3.5 w-3.5" />
                      Tests
                    </Button>
                    {structuredLogs?.other &&
                      structuredLogs.other.length > 0 && (
                        <Button
                          type="button"
                          variant="ghost"
                          size="sm"
                          onClick={() => setLogCategory("other")}
                          className={cn(
                            "w-full justify-start gap-2 px-2 py-1.5 text-xs",
                            logCategory === "other"
                              ? "bg-primary/10 text-primary font-medium"
                              : "text-muted-foreground hover:bg-muted",
                          )}
                        >
                          <FileText className="h-3.5 w-3.5" />
                          Other
                        </Button>
                      )}
                    {(structuredLogs?.exception || trial.error_message) && (
                      <Button
                        type="button"
                        variant="ghost"
                        size="sm"
                        onClick={() => setLogCategory("exception")}
                        className={cn(
                          "w-full justify-start gap-2 px-2 py-1.5 text-xs",
                          logCategory === "exception"
                            ? "bg-red-500/10 text-red-500 font-medium"
                            : "text-red-500/70 hover:bg-red-500/10",
                        )}
                      >
                        <Flame className="h-3.5 w-3.5" />
                        Error
                      </Button>
                    )}
                  </div>

                  <div className="flex-1 overflow-auto p-3">
                    {logCategory === "agent" &&
                      (() => {
                        const agentTabs: {
                          id: string;
                          label: string;
                          content: string;
                          lang: string;
                        }[] = [];
                        if (structuredLogs?.agent.oracle)
                          agentTabs.push({
                            id: "oracle",
                            label: "Oracle",
                            content: structuredLogs.agent.oracle,
                            lang: "text",
                          });
                        if (structuredLogs?.agent.setup)
                          agentTabs.push({
                            id: "setup",
                            label: "Setup",
                            content: structuredLogs.agent.setup,
                            lang: "bash",
                          });
                        for (const cmd of structuredLogs?.agent.commands ?? [])
                          agentTabs.push({
                            id: `cmd-${cmd.name}`,
                            label: cmd.name,
                            content: cmd.content,
                            lang: "bash",
                          });

                        if (agentTabs.length === 0)
                          return (
                            <div className="text-center py-8 text-muted-foreground text-sm">
                              No agent logs available
                            </div>
                          );

                        return (
                          <Tabs defaultValue={agentTabs[0].id}>
                            <TabsList className="h-8 bg-muted/50 flex-wrap">
                              {agentTabs.map((tab) => (
                                <TabsTrigger
                                  key={tab.id}
                                  value={tab.id}
                                  className="text-xs px-3 py-1"
                                >
                                  {tab.label}
                                </TabsTrigger>
                              ))}
                            </TabsList>
                            {agentTabs.map((tab) => (
                              <TabsContent
                                key={tab.id}
                                value={tab.id}
                                className="mt-2"
                              >
                                <CodeBlock
                                  code={tab.content}
                                  language={tab.lang}
                                  maxHeight="24rem"
                                />
                              </TabsContent>
                            ))}
                          </Tabs>
                        );
                      })()}

                    {logCategory === "verifier" && (
                      <div className="space-y-3">
                        {structuredLogs?.verifier.stdout ||
                        structuredLogs?.verifier.stderr ? (
                          <Tabs
                            defaultValue={
                              structuredLogs?.verifier.stdout
                                ? "stdout"
                                : "stderr"
                            }
                          >
                            <TabsList className="h-8 bg-muted/50">
                              {structuredLogs?.verifier.stdout && (
                                <TabsTrigger
                                  value="stdout"
                                  className="text-xs px-3 py-1"
                                >
                                  Output
                                </TabsTrigger>
                              )}
                              {structuredLogs?.verifier.stderr && (
                                <TabsTrigger
                                  value="stderr"
                                  className="text-xs px-3 py-1"
                                >
                                  Stderr
                                </TabsTrigger>
                              )}
                            </TabsList>
                            {structuredLogs?.verifier.stdout && (
                              <TabsContent value="stdout" className="mt-2">
                                <CodeBlock
                                  code={structuredLogs.verifier.stdout}
                                  language="text"
                                  maxHeight="24rem"
                                />
                              </TabsContent>
                            )}
                            {structuredLogs?.verifier.stderr && (
                              <TabsContent value="stderr" className="mt-2">
                                <CodeBlock
                                  code={structuredLogs.verifier.stderr}
                                  language="text"
                                  maxHeight="24rem"
                                />
                              </TabsContent>
                            )}
                          </Tabs>
                        ) : (
                          <div className="text-center py-8 text-muted-foreground text-sm">
                            No test output available
                          </div>
                        )}
                      </div>
                    )}

                    {logCategory === "other" && (
                      <div className="space-y-3">
                        {structuredLogs?.other.map((log, idx) => (
                          <div key={idx}>
                            <h4 className="text-[10px] font-semibold text-muted-foreground uppercase tracking-wider mb-1.5">
                              {log.name}
                            </h4>
                            <CodeBlock
                              code={log.content}
                              language="text"
                            />
                          </div>
                        ))}
                        {(!structuredLogs?.other ||
                          structuredLogs.other.length === 0) && (
                          <div className="text-center py-8 text-muted-foreground text-sm">
                            No other logs available
                          </div>
                        )}
                      </div>
                    )}

                    {logCategory === "exception" && (
                      <CodeBlock
                        code={
                          structuredLogs?.exception ||
                          trial.error_message ||
                          "No exception details"
                        }
                        language="text"
                      />
                    )}
                  </div>
                </>
              )}
            </div>
          </TabsContent>

          <TabsContent value="files" className="m-0 h-full p-0">
            <TaskFilesPanel
              isOpen={isOpen && activeTab === "files"}
              onClose={() => {}}
              taskId={null}
              filesUrl={`${apiBaseUrl}/trials/${trial.id}/files`}
              contentOnly
            />
          </TabsContent>

          <TabsContent value="artifacts" className="m-0 h-full p-0 overflow-auto">
            <ArtifactsViewer
              filesUrl={`${apiBaseUrl}/trials/${trial.id}/files`}
            />
          </TabsContent>

          <TabsContent
            value="trajectory"
            className="m-0 h-full p-0 overflow-auto"
          >
            <TrajectoryViewer trialId={trial.id} apiBaseUrl={apiBaseUrl} />
          </TabsContent>
        </div>
      </Tabs>
    </>
  );

  if (contentOnly) {
    return (
      <div className="flex-1 flex flex-col overflow-hidden h-full">
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
