"use client";

import { useState, useEffect, useCallback, useMemo, useRef } from "react";
import dynamic from "next/dynamic";
import Image from "next/image";
import {
  ResizableDrawer,
  DrawerHeader,
  DrawerTitle,
  DrawerDescription,
} from "@/components/ui/resizable-drawer";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import {
  FileText,
  FolderOpen,
  AlertCircle,
  CircleSlash,
  ChevronDown,
  ChevronUp,
  ChevronLeft,
  ChevronRight,
  RotateCcw,
  Loader2,
  Radio,
  Microscope,
  CheckCircle2,
  XCircle,
  ExternalLink,
  Route,
  Package,
  Trash2,
} from "lucide-react";
import { cn, encodeExperimentRouteParam, urlWithSearch } from "@/lib/utils";
import {
  formatLineRange,
  parseLineRange,
  type LineRange,
} from "@/lib/line-range";
import { sameFilePath } from "@/lib/file-path";

/**
 * Read a query param from the live URL. The panel keeps the URL current
 * via replaceState, which never refreshes Next's useSearchParams hook —
 * so live reads must come straight from location.
 */
function getLiveParam(name: string): string | null {
  if (typeof window === "undefined") return null;
  return new URLSearchParams(window.location.search).get(name);
}
import { Skeleton } from "@/components/ui/skeleton";
import { QaAssessmentReport } from "@/components/qa-report/qa-assessment-report";
import type { FeedbackRecord } from "@/components/qa-report/types";
import { TimingBreakdownBar } from "@/components/timing-breakdown-bar";
import { CodeBlock } from "@/components/code-block";
import type { Trial, Task } from "@/lib/types";
import { isAgentTrial } from "@/lib/types";
import { stepIdsLabel } from "@/lib/trajectory-segments";
import {
  costEstimateMarks,
  formatCostUsd,
  formatTokenCount,
  hasDisplayableCostUsd,
  sumTaskTrialCost,
} from "@/lib/format";
import {
  formatPartialRewardBadgeValue,
  formatRewardPercent,
  formatRewardValue,
  getMatrixStatus,
  getRewardStyle,
  STATUS_CONFIG,
  STATUS_GLYPH_BOX,
  type MatrixStatus,
} from "@/lib/status-config";
import { HarborStageTimeline } from "@/components/harbor-stage-timeline";
import { HarborStageBadge } from "@/components/harbor-stage-badge";
import { QueueKeyIcon } from "@/components/queue-key-icon";
import { StatusIcon } from "@/components/status-icon";
import { QaCostSuffix } from "@/components/qa-cost-suffix";
import { TrialNotRealSpendBadge } from "@/components/not-real-spend-badge";
import {
  isActiveTrialStatus,
  isLiveQaTrial,
  isWorkerOwnedTrialStatus,
  taskHasActiveVerdict,
} from "@/lib/job-status";
import { isAnalysisStatusActive, useTrial } from "@/lib/use-trial";
import { embeddedCtrfSummary } from "@/lib/verifier-results";
import { fetcher } from "@/lib/api";

const TaskFilesPanel = dynamic(
  () =>
    import("@/components/task-files-panel").then((mod) => mod.TaskFilesPanel),
  {
    ssr: false,
    loading: () => <DrawerPanelLoading label="Loading files..." />,
  }
);

const LiveTranscriptPanel = dynamic(
  () =>
    import("@/components/live-transcript-panel").then(
      (mod) => mod.LiveTranscriptPanel
    ),
  {
    ssr: false,
    loading: () => <DrawerPanelLoading label="Loading live transcript..." />,
  }
);

const ArtifactsViewer = dynamic(
  () =>
    import("@/components/artifacts-viewer").then((mod) => mod.ArtifactsViewer),
  {
    ssr: false,
    loading: () => <DrawerPanelLoading label="Loading artifacts..." />,
  }
);

const TrajectoryViewer = dynamic(
  () =>
    import("@/components/trajectory-viewer").then(
      (mod) => mod.TrajectoryViewer
    ),
  {
    ssr: false,
    loading: () => <DrawerPanelLoading label="Loading trajectory..." />,
  }
);

function DrawerPanelLoading({ label }: { label: string }) {
  return (
    <div className="flex min-h-[160px] flex-col gap-2 p-4">
      <span className="sr-only">{label}</span>
      <Skeleton className="h-4 w-1/3" />
      <Skeleton className="h-3 w-full" />
      <Skeleton className="h-3 w-11/12" />
      <Skeleton className="h-3 w-4/5" />
      <Skeleton className="mt-2 h-24 w-full rounded-lg" />
    </div>
  );
}

function ActiveTabContent({
  active,
  ...props
}: React.ComponentProps<typeof TabsContent> & { active: boolean }) {
  return active ? <TabsContent {...props} /> : null;
}

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
  onNavigate?: (trial: Trial, trialIndex: number | null) => void;
  onNavigateToTask?: () => void;
  onRetry?: (taskIds?: string[]) => void | Promise<void>;
  onDelete?: (trial: Trial, task: Task | null) => Promise<void>;
  apiBaseUrl?: string;
  allowRetry?: boolean;
  /**
   * When false, the analysis card and run-analysis action are hidden
   * entirely — used by the public read-only share view.
   */
  showAnalysis?: boolean;
  /** Load fields omitted from compact task and experiment trial rows. */
  requireTrialDetail?: boolean;
  allowDelete?: boolean;
  /** Render content only without ResizableDrawer wrapper */
  contentOnly?: boolean;
  /** Slot rendered alongside the navigation row — e.g. a "hide task" toggle. */
  paneAction?: React.ReactNode;
}

// Hardcoded feature flag: shows the per-trial "Re-run analysis" button on the
// QA card. Testing-only for now — flip to false to hide it.
const ENABLE_RERUN_ANALYSIS_BUTTON = true;

const OUTCOME_CARD_TONE: Record<MatrixStatus, string> = {
  pass: "border-emerald-500/30 bg-emerald-500/10",
  partial: "border-amber-500/30 bg-amber-500/10",
  fail: "border-red-500/30 bg-red-500/10",
  "harness-error": "border-yellow-500/30 bg-yellow-500/10",
  scoreless: "border-slate-500/30 bg-slate-500/10",
  skipped: "border-slate-500/25 bg-slate-500/5",
  pending: "border-gray-500/30 bg-gray-500/10",
  queued: "border-purple-500/30 bg-purple-500/10",
  running: "border-blue-500/30 bg-blue-500/10",
  paused: "border-amber-500/30 bg-amber-500/10",
};

// The QA assessment card. It renders before any analysis exists, so the
// run button is reachable. It shows queued/running state with elapsed
// time. The drawer's useTrial resource makes the result appear without
// reopening the drawer.
function TrialAnalysisCard({
  trial: trialProp,
  taskQaInProgress,
  apiBaseUrl,
  actionsReady,
  onQueued,
  activeQaTrial,
  onOpenActiveQaTrial,
  onFeedback,
}: {
  trial: Trial;
  taskQaInProgress: boolean;
  apiBaseUrl: string;
  actionsReady: boolean;
  onQueued?: () => void | Promise<void>;
  activeQaTrial: Trial | null;
  onOpenActiveQaTrial?: (qaTrial: Trial) => void;
  onFeedback?: (record: FeedbackRecord) => Promise<void>;
}) {
  const trial = trialProp;
  const [queuing, setQueuing] = useState(false);
  const [queueError, setQueueError] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now());

  // QA is task-scoped: the rerun creates one qa trial that grades every
  // trial, and never stamps this row's analysis_status. Reading that field
  // alone showed "No analysis yet" while the run was live.
  const trialAnalysisInProgress = isAnalysisStatusActive(trial.analysis_status);
  const inProgress = trialAnalysisInProgress || taskQaInProgress;
  // Tick the elapsed timer once a second while in progress.
  useEffect(() => {
    if (!inProgress) return;
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [inProgress]);

  const hasAnalysis =
    Boolean(trial.analysis_status || trial.analysis) || inProgress;
  // Always SHOW the button when the feature is on. Disable it (with the
  // reason) when a run cannot be queued right now. The action targets this
  // selected agent trial, while active task-level QA blocks concurrent runs.
  const showQueueButton = ENABLE_RERUN_ANALYSIS_BUTTON;
  // Mirrors _ANALYSIS_CLAIM_TTL_MINUTES: past the lease the backend treats
  // the worker as dead and allows a re-run, so the button must too. A
  // running row with no start time has no live lease, and the backend
  // allows a re-run there as well.
  const runStale =
    trial.analysis_status === "running" &&
    (trial.analysis_started_at == null ||
      now - new Date(trial.analysis_started_at).getTime() > 35 * 60_000);
  // Mirror the backend guards, so the button is disabled with the reason
  // instead of failing the request.
  let queueBlockedReason: string | null = null;
  if (!actionsReady) {
    queueBlockedReason = "Loading latest trial state.";
  } else if (taskQaInProgress) {
    queueBlockedReason = "Task-level QA is already running";
  } else if (trialAnalysisInProgress && !runStale) {
    queueBlockedReason =
      trial.analysis_status === "running"
        ? "Analysis is already running for this trial"
        : "Analysis is already queued for this trial";
  } else if (trial.status !== "success" && trial.status !== "failed") {
    queueBlockedReason = "The trial must finish before analysis can run";
  }

  if (!hasAnalysis && !showQueueButton) return null;

  const queueRun = async () => {
    if (queuing || !actionsReady || queueBlockedReason) return;
    setQueuing(true);
    setQueueError(null);
    try {
      const res = await fetch(
        `${apiBaseUrl}/trials/${trial.id}/analysis/rerun`,
        { method: "POST" }
      );
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(
          data.detail || data.error || "Failed to queue analysis"
        );
      }
      // The server created one task-level QA trial. Refresh the task-open
      // resource so its active_qa_trial becomes the source of truth.
      await onQueued?.();
    } catch (err) {
      setQueueError(
        err instanceof Error ? err.message : "Failed to queue analysis"
      );
    } finally {
      setQueuing(false);
    }
  };

  // Progress line: elapsed time while running, waiting state while queued.
  let progressLine: string | null = null;
  if (inProgress) {
    if (trial.analysis_status === "running") {
      if (trial.analysis_started_at) {
        const secs = Math.max(
          0,
          Math.floor(
            (now - new Date(trial.analysis_started_at).getTime()) / 1000
          )
        );
        progressLine = `Running for ${Math.floor(secs / 60)}m ${secs % 60}s.`;
      }
    } else {
      progressLine = "Waiting for a QA worker.";
    }
  }

  // Analysis wall-clock, shown in the report header.
  let analysisDuration: string | null = null;
  if (trial.analysis_started_at && trial.analysis_finished_at) {
    const secs = Math.round(
      (new Date(trial.analysis_finished_at).getTime() -
        new Date(trial.analysis_started_at).getTime()) /
        1000
    );
    if (Number.isFinite(secs) && secs >= 0) {
      analysisDuration =
        secs >= 60 ? `${Math.floor(secs / 60)}m ${secs % 60}s` : `${secs}s`;
    }
  }

  const showReport =
    hasAnalysis && !inProgress && !!trial.analysis?.classification;

  return (
    <Card
      className={
        inProgress
          ? "border-blue-500/30 bg-blue-500/5"
          : showReport
            ? // The report card carries the verdict tint itself.
              "border-border/60"
            : "border-slate-500/30 bg-slate-500/5"
      }
    >
      <CardContent className="px-4 py-3">
        {showQueueButton && (
          <div className="mb-2 flex justify-end">
            <button
              type="button"
              disabled={queuing || queueBlockedReason !== null}
              onClick={queueRun}
              className="text-muted-foreground hover:text-foreground rounded border px-1.5 py-0.5 text-[10px] font-medium disabled:cursor-not-allowed disabled:opacity-50"
              title={
                queueBlockedReason ??
                (hasAnalysis
                  ? "Reset this trial's analysis and re-run it with the latest prompt"
                  : "Analyze this trial with the latest prompt")
              }
            >
              {queuing
                ? "Queuing…"
                : hasAnalysis
                  ? "Re-run analysis"
                  : "Run analysis"}
            </button>
          </div>
        )}
        {queueError && (
          <p className="mb-2 text-[11px] text-red-500">{queueError}</p>
        )}
        {/* Disabled buttons swallow hover, so the title alone never shows.
            While a run is in progress the card body already says so. */}
        {queueBlockedReason && !inProgress && (
          <p className="text-muted-foreground mb-2 text-[11px]">
            {queueBlockedReason}
          </p>
        )}
        {showReport ? (
          <>
            {trial.analysis_status === "failed" && trial.analysis_error && (
              <p className="mb-2 text-xs text-red-500">
                Analysis failed: {trial.analysis_error}
              </p>
            )}
            <QaAssessmentReport
              key={trial.id}
              classification={trial.analysis!.classification!}
              subtype={trial.analysis?.subtype}
              rootCause={trial.analysis?.root_cause || trial.analysis?.evidence}
              recommendation={trial.analysis?.recommendation}
              evidence={
                // Without a root cause the evidence IS the lead text above;
                // passing it again would render the same prose twice.
                trial.analysis?.root_cause &&
                trial.analysis?.evidence &&
                trial.analysis.evidence !== trial.analysis.root_cause
                  ? trial.analysis.evidence
                  : null
              }
              actionItems={trial.analysis?.action_items}
              duration={analysisDuration}
              raw={trial.analysis}
              onFeedback={onFeedback}
            />
            {trial.analysis?._graded_by && (
              <p className="mt-2 flex flex-wrap items-baseline gap-x-2 font-mono text-[11px]">
                <a
                  href={`/tasks/${encodeURIComponent(trial.task_id)}?trial=${encodeURIComponent(trial.analysis._graded_by)}&tab=trajectory`}
                  className="text-muted-foreground hover:text-foreground underline decoration-dotted underline-offset-2"
                >
                  graded by {trial.analysis._graded_by}
                </a>
                {(trial.analysis._graded_at_steps?.length ?? 0) > 0 && (
                  // A plain anchor on purpose: the trajectory viewer captures
                  // the #step- fragment at mount, which document navigation
                  // guarantees and in-place drawer switching does not.
                  <a
                    href={`/tasks/${encodeURIComponent(trial.task_id)}?trial=${encodeURIComponent(trial.analysis._graded_by)}&tab=trajectory#step-${trial.analysis._graded_at_steps![0]}`}
                    title="Open the QA run's trajectory at the steps that judged this trial"
                    className="text-muted-foreground hover:text-foreground underline decoration-dotted underline-offset-2"
                  >
                    at {stepIdsLabel(trial.analysis._graded_at_steps!)}
                  </a>
                )}
              </p>
            )}
          </>
        ) : (
          <div className="flex items-start gap-3">
            {inProgress ? (
              <Microscope className="mt-0.5 h-5 w-5 animate-pulse text-blue-500" />
            ) : hasAnalysis ? (
              <XCircle className="mt-0.5 h-5 w-5 text-slate-500" />
            ) : (
              <Microscope className="mt-0.5 h-5 w-5 text-slate-400" />
            )}
            <div className="min-w-0 flex-1">
              {inProgress ? (
                <div className="flex flex-col gap-1">
                  <span className="font-mono text-sm font-bold">
                    {trial.analysis_status === "running"
                      ? "Analyzing"
                      : trial.analysis_status
                        ? "Analysis queued"
                        : "QA is running"}
                  </span>
                  <span className="text-muted-foreground text-xs">
                    {trial.analysis_status
                      ? progressLine
                      : "The task's QA run grades every trial; this trial's result lands when it finishes."}
                  </span>
                  {!trial.analysis_status &&
                    activeQaTrial &&
                    onOpenActiveQaTrial && (
                      <button
                        type="button"
                        onClick={() => onOpenActiveQaTrial(activeQaTrial)}
                        className="text-muted-foreground hover:text-foreground self-start font-mono text-[11px] underline decoration-dotted underline-offset-2"
                      >
                        view the QA run
                      </button>
                    )}
                </div>
              ) : hasAnalysis ? (
                // Analysis state exists but produced no report (e.g. failed
                // before the classifier returned).
                <div className="flex flex-col gap-1">
                  <span className="font-mono text-sm font-bold">Analysis</span>
                  {trial.analysis_status === "failed" &&
                  trial.analysis_error ? (
                    <span className="text-xs text-red-500">
                      Analysis failed: {trial.analysis_error}
                    </span>
                  ) : (
                    <span className="text-muted-foreground text-xs">
                      No report was produced.
                    </span>
                  )}
                </div>
              ) : (
                <div className="flex flex-col gap-1">
                  <span className="font-mono text-sm font-bold">
                    No analysis yet
                  </span>
                  <span className="text-muted-foreground text-xs">
                    This trial has not been analyzed.
                  </span>
                </div>
              )}
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

export function buildOddishRunCommand(trial: Trial, task: Task): string {
  const parts: string[] = ["oddish run"];

  // `--task <task_id>` re-queues trials against the existing server-side
  // task, so it works even when the user doesn't have the task files locally.
  // Tasks have a many-to-many relationship with experiments (see
  // `task_experiments` in oddish/db/models.py), so we pass `--experiment`
  // explicitly to make sure new trials land in the experiment the user was
  // viewing rather than the task's oldest linked experiment.
  if (task.id) {
    parts.push(`--task ${task.id}`);
  }

  if (task.experiment_id) {
    parts.push(`--experiment ${task.experiment_id}`);
  }

  const sandboxBackend = getSandboxBackend(trial);
  if (sandboxBackend) {
    parts.push(`-e ${sandboxBackend.id}`);
  }

  if (trial.agent) {
    parts.push(`-a ${trial.agent}`);
  }

  if (trial.model) {
    parts.push(`-m ${trial.queue_key || trial.model}`);
  }

  return parts.join(" ");
}

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

function hasLiveQueueSnapshot(trial: Trial): boolean {
  return isActiveTrialStatus(trial.status);
}

type SandboxBackendId = "daytona" | "modal" | "archil" | "ec2" | "numinous";

type SandboxBackend = {
  id: SandboxBackendId;
  label: string;
  logoSrc?: string;
  logoWidth?: number;
  // When true the logo is a full square mark and fills the badge edge to edge
  // (no white plate, no padding), e.g. an app-icon style logo.
  logoFill?: boolean;
  href?: string;
};

const SANDBOX_BACKENDS: Record<
  SandboxBackendId,
  Omit<SandboxBackend, "href">
> = {
  daytona: {
    id: "daytona",
    label: "Daytona",
    logoSrc: "/daytona-logotype.svg",
    logoWidth: 50,
  },
  modal: {
    id: "modal",
    label: "Modal",
    logoSrc: "/modal-logo-icon.png",
    logoWidth: 10,
  },
  archil: {
    id: "archil",
    label: "Archil",
  },
  ec2: {
    id: "ec2",
    label: "EC2",
  },
  numinous: {
    id: "numinous",
    label: "Numinous Cloud",
    logoSrc: "/numinous-logo.png",
    logoFill: true,
  },
};

function normalizeSandboxBackend(
  provider: string | null | undefined
): SandboxBackendId | null {
  const normalized = provider?.trim().toLowerCase();
  if (
    normalized === "daytona" ||
    normalized === "modal" ||
    normalized === "archil" ||
    normalized === "ec2" ||
    normalized === "numinous"
  ) {
    return normalized;
  }
  return null;
}

function getSandboxBackend(trial: Trial): SandboxBackend | null {
  const trialBackendId = normalizeSandboxBackend(trial.environment);
  const sandboxJob = trial.jobs?.find((job) =>
    Boolean(normalizeSandboxBackend(job.provider))
  );
  const backendId =
    trialBackendId ?? normalizeSandboxBackend(sandboxJob?.provider);
  if (!backendId) return null;

  const backend = SANDBOX_BACKENDS[backendId];
  if (backendId === "daytona" && sandboxJob?.external_id) {
    return {
      ...backend,
      href: `https://app.daytona.io/dashboard/sandboxes?sandboxId=${encodeURIComponent(
        sandboxJob.external_id
      )}`,
    };
  }

  return backend;
}

function SandboxBackendBadge({ backend }: { backend: SandboxBackend }) {
  const content = (
    <>
      {backend.logoSrc &&
        (backend.logoFill ? (
          <span className="inline-flex h-4 w-4 items-center justify-center overflow-hidden rounded-sm">
            <Image
              src={backend.logoSrc}
              alt={`${backend.label} logo`}
              width={16}
              height={16}
              className="h-full w-full object-cover"
            />
          </span>
        ) : (
          <span className="inline-flex h-4 items-center justify-center rounded-sm bg-white px-1">
            <Image
              src={backend.logoSrc}
              alt={`${backend.label} logo`}
              width={backend.logoWidth ?? 10}
              height={10}
              className="h-2.5 w-auto object-contain"
            />
          </span>
        ))}
      {backend.id !== "daytona" && (
        <span className="text-muted-foreground font-sans text-[9px] font-semibold tracking-wide uppercase">
          {backend.label}
        </span>
      )}
    </>
  );
  const className =
    "border-border bg-muted/40 inline-flex shrink-0 items-center gap-1 rounded-md border px-1 py-0.5";

  if (backend.href) {
    return (
      <a
        href={backend.href}
        target="_blank"
        rel="noopener noreferrer"
        className={className}
        title={`Open ${backend.label} sandbox`}
      >
        {content}
      </a>
    );
  }

  return (
    <span className={className} title={`${backend.label} sandbox`}>
      {content}
    </span>
  );
}

/**
 * Short "org/repo" label for the Harbor source a trial executed against.
 * Strips a leading git+ and the github host so the drawer shows the ACTUAL
 * source (default, a blessed variant, or an arbitrary override), not a
 * hardcoded fork. Falls back to "harbor" for legacy rows with no stamped source.
 */
function harborRepoLabel(source: string | null | undefined): string {
  if (!source) return "harbor";
  return (
    source
      .replace(/^git\+/, "")
      .replace(/^https?:\/\/github\.com\//i, "")
      .replace(/\.git$/, "") || "harbor"
  );
}

export function TrialDetailPanel({
  isOpen,
  onClose,
  trial: selectedTrial,
  task,
  orderedTrials,
  trialIndex,
  trialGroups,
  onNavigate,
  onNavigateToTask,
  onRetry,
  onDelete,
  apiBaseUrl = "/api",
  allowRetry = true,
  showAnalysis = true,
  requireTrialDetail = true,
  allowDelete = false,
  contentOnly = false,
  paneAction,
}: TrialDetailPanelProps) {
  const taskQaInProgress = taskHasActiveVerdict(task);
  const {
    data: refreshedTrial,
    error: trialDetailError,
    isValidating: isValidatingTrialDetail,
    mutate: revalidateTrial,
  } = useTrial(isOpen && requireTrialDetail ? selectedTrial?.id : null, {
    apiBaseUrl,
  });
  const previousTaskQaRef = useRef({
    taskId: task?.id ?? null,
    inProgress: taskQaInProgress,
  });

  // A task-level QA trial writes analysis onto the selected agent trial when
  // it settles. The agent trial is already terminal, so its own status cannot
  // keep useTrial polling. Refetch it once when the task QA lifecycle ends;
  // SWR still owns the request, cache key, deduplication, and retry policy.
  useEffect(() => {
    const previous = previousTaskQaRef.current;
    const current = {
      taskId: task?.id ?? null,
      inProgress: taskQaInProgress,
    };
    previousTaskQaRef.current = current;

    if (
      isOpen &&
      requireTrialDetail &&
      selectedTrial?.id &&
      previous.taskId === current.taskId &&
      previous.inProgress &&
      !current.inProgress
    ) {
      void revalidateTrial();
    }
  }, [
    isOpen,
    requireTrialDetail,
    revalidateTrial,
    selectedTrial?.id,
    task?.id,
    taskQaInProgress,
  ]);
  const canonicalTrial =
    refreshedTrial?.id === selectedTrial?.id ? refreshedTrial : null;
  const trialDetailFailed =
    requireTrialDetail && canonicalTrial === null && trialDetailError != null;
  const actionsReady = !requireTrialDetail || canonicalTrial !== null;
  const trial = canonicalTrial ?? selectedTrial;
  const verifierSummary = embeddedCtrfSummary(trial?.result);

  // QA votes require authenticated routes and an experiment anchor. Public
  // share drawers use a different apiBaseUrl, so they do not render controls.
  const feedbackExperimentId =
    apiBaseUrl === "/api" ? (trial?.experiment_id ?? null) : null;
  async function handleQaFeedback(record: FeedbackRecord): Promise<void> {
    if (!feedbackExperimentId || !trial) {
      throw new Error("QA feedback is unavailable for this trial");
    }
    await fetcher(
      `/api/experiments/${encodeExperimentRouteParam(feedbackExperimentId)}/feedback`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          body: record.note?.trim() ?? "",
          target:
            record.target.kind === "verdict" ? "qa_verdict" : "qa_action_item",
          target_key:
            record.target.kind === "verdict"
              ? record.target.classification
              : record.target.id,
          vote: record.vote,
          trial_id: trial.id,
        }),
      }
    );
  }

  const validTabs = useMemo(
    () => new Set(["summary", "live", "files", "trajectory", "artifacts"]),
    []
  );

  const [activeTab, setActiveTab] = useState(() => {
    const urlTab = getLiveParam("tab");
    return urlTab && validTabs.has(urlTab) ? urlTab : "summary";
  });
  const [showFullError, setShowFullError] = useState(false);
  const [retrying, setRetrying] = useState(false);
  const [retryError, setRetryError] = useState<string | null>(null);
  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  // ``?file=`` / ``?lines=`` are scoped by ``?tab=``: on the artifacts tab
  // they address the artifact browser, otherwise the files tab. Each tab
  // keeps its own state; the URL carries the active tab's pair.
  const [filesTargetPath, setFilesTargetPath] = useState<string | null>(() =>
    getLiveParam("tab") === "artifacts" ? null : getLiveParam("file")
  );
  // Line-anchor range within the selected file (``?lines=L12-L20``).
  const [selectedLines, setSelectedLines] = useState<LineRange | null>(() =>
    getLiveParam("tab") === "artifacts"
      ? null
      : parseLineRange(getLiveParam("lines"))
  );
  const [artifactsTargetPath, setArtifactsTargetPath] = useState<string | null>(
    () => (getLiveParam("tab") === "artifacts" ? getLiveParam("file") : null)
  );
  const [artifactsLines, setArtifactsLines] = useState<LineRange | null>(() =>
    getLiveParam("tab") === "artifacts"
      ? parseLineRange(getLiveParam("lines"))
      : null
  );

  const hydratedFromUrl = useRef(false);

  // Hydrate from the URL on first open. Reads the live URL, not the
  // useSearchParams snapshot: replaceState never refreshes that hook, so on
  // a remount (e.g. the drawer's pane layout changing) the snapshot is the
  // stale page-load query and would reset the panel to where the user
  // started instead of where they are.
  useEffect(() => {
    if (!isOpen || hydratedFromUrl.current) return;
    hydratedFromUrl.current = true;
    const urlTab = getLiveParam("tab");
    const urlFile = getLiveParam("file");
    const urlLines = parseLineRange(getLiveParam("lines"));
    if (urlTab && validTabs.has(urlTab)) setActiveTab(urlTab);
    if (urlTab === "artifacts") {
      // ?file=/?lines= address the artifact browser while tab=artifacts.
      if (urlFile) {
        setArtifactsTargetPath(urlFile);
        artifactsTargetPathRef.current = urlFile;
      }
      if (urlLines) setArtifactsLines(urlLines);
      return;
    }
    if (urlFile) {
      setFilesTargetPath(urlFile);
      filesTargetPathRef.current = urlFile;
      if (!urlTab) setActiveTab("files");
    }
    if (urlLines) setSelectedLines(urlLines);
  }, [isOpen, validTabs]);

  // The file viewer reports every selection change (tree clicks and
  // auto-selects alike): the ?file= param stays live, and switching to a
  // different file drops the line anchor — the old range would otherwise
  // highlight arbitrary lines of the new file. The ref mirrors the state
  // so the comparison doesn't need an impure setState updater.
  const filesTargetPathRef = useRef<string | null>(filesTargetPath);
  const handleSelectedFileChange = useCallback((path: string | null) => {
    if (!sameFilePath(filesTargetPathRef.current, path)) setSelectedLines(null);
    filesTargetPathRef.current = path;
    setFilesTargetPath(path);
  }, []);

  // Same shape for the artifacts tab: its browser reports selections the
  // same way, and a different file drops the artifact line anchor. The
  // comparison also accepts the file's storage path — a deep link can
  // address a multi-step artifact by storage path, and the browser echoes
  // back the relativized tree path, which is not a suffix match of it.
  const artifactsTargetPathRef = useRef<string | null>(artifactsTargetPath);
  const handleArtifactsFileChange = useCallback(
    (path: string | null, fullPath?: string) => {
      const prev = artifactsTargetPathRef.current;
      const same =
        sameFilePath(prev, path) ||
        (fullPath !== undefined && sameFilePath(prev, fullPath));
      if (!same) setArtifactsLines(null);
      artifactsTargetPathRef.current = path;
      setArtifactsTargetPath(path);
    },
    []
  );

  // Navigating to a different trial keeps the file paths (attempts share
  // layouts, and comparing the same file across attempts is the point) but
  // drops the line anchors — they addressed the previous trial's content
  // and would highlight arbitrary lines here.
  const lastTrialIdRef = useRef<string | null>(trial?.id ?? null);
  useEffect(() => {
    const id = trial?.id ?? null;
    if (id && lastTrialIdRef.current && id !== lastTrialIdRef.current) {
      setSelectedLines(null);
      setArtifactsLines(null);
    }
    lastTrialIdRef.current = id;
  }, [trial?.id]);

  // Sync tab, file & lines to URL (without triggering router navigation).
  // Based on the live URL rather than the useSearchParams snapshot:
  // replaceState doesn't refresh that hook, and the experiment view writes
  // its own params (task/trial/taskFile/taskLines) the same way — a stale
  // base would silently wipe them. The tab is written explicitly even for
  // summary, so every tab is its own address and every tab click visibly
  // updates the URL.
  useEffect(() => {
    if (!isOpen || !hydratedFromUrl.current) return;
    const current = new URLSearchParams(window.location.search);
    const next = new URLSearchParams(window.location.search);

    if (activeTab) {
      next.set("tab", activeTab);
    } else {
      next.delete("tab");
    }

    // ?file=/?lines= describe the active tab's view: the files tab's pair
    // or the artifacts tab's pair. On tabs with no file view (summary,
    // live, trajectory) they drop out of the URL — the tab states stay in
    // React, so flipping back restores and re-writes them.
    const paneFile =
      activeTab === "artifacts"
        ? artifactsTargetPath
        : activeTab === "files"
          ? filesTargetPath
          : null;
    const paneLines =
      activeTab === "artifacts"
        ? artifactsLines
        : activeTab === "files"
          ? selectedLines
          : null;

    if (paneFile) {
      next.set("file", paneFile);
    } else {
      next.delete("file");
    }

    if (paneLines) {
      next.set("lines", formatLineRange(paneLines));
    } else {
      next.delete("lines");
    }

    if (next.toString() !== current.toString()) {
      const url = urlWithSearch(next.toString());
      window.history.replaceState(window.history.state, "", url);
    }
  }, [
    isOpen,
    activeTab,
    filesTargetPath,
    selectedLines,
    artifactsTargetPath,
    artifactsLines,
  ]);

  // Agent rows only: the generic retry endpoint refuses qa/audit kinds, so
  // offering the button on their drawers would only ever render its 400.
  const showRetry =
    allowRetry &&
    Boolean(trial && isAgentTrial(trial)) &&
    (trial?.status === "failed" || trial?.status === "success");
  const canRetry = actionsReady && showRetry;
  const showDelete = allowDelete && Boolean(onDelete) && Boolean(trial);
  const canDelete = actionsReady && showDelete;
  const handleRetry = async () => {
    if (!trial || retrying || !canRetry) return;
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

  const handleDelete = async () => {
    if (!trial || !onDelete || deleting || !canDelete) return;
    setDeleting(true);
    setDeleteError(null);

    try {
      await onDelete(trial, task);
      setDeleteDialogOpen(false);
      onClose();
    } catch (err) {
      setDeleteError(
        err instanceof Error ? err.message : "Failed to delete trial"
      );
    } finally {
      setDeleting(false);
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
    // Through the shared handler so a file change drops the old line
    // anchor and the path ref stays in sync — a bare setFilesTargetPath
    // would let the previous ?lines= range land on the new file.
    handleSelectedFileChange(filePath);
  };

  // Reset state when panel closes
  useEffect(() => {
    if (!isOpen) {
      setActiveTab("summary");
      setShowFullError(false);
      setRetrying(false);
      setRetryError(null);
      setDeleteDialogOpen(false);
      setDeleting(false);
      setDeleteError(null);
      setFilesTargetPath(null);
      hydratedFromUrl.current = false;
    }
  }, [isOpen]);

  const orderedList = useMemo(
    () => orderedTrials ?? task?.trials ?? [],
    [orderedTrials, task?.trials]
  );
  const activeQaTrial =
    task?.active_qa_trial ?? task?.trials?.find(isLiveQaTrial) ?? null;
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
    [onNavigate, orderedList]
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
    trial.error_message
  );
  const showLive =
    isWorkerOwnedTrialStatus(trial.status) || trial.status === "retrying";
  const effectiveTab =
    activeTab === "live" && !showLive ? "summary" : activeTab;
  const trialDetailErrorContent = (
    <div className="p-4 sm:p-6">
      <Alert variant="destructive">
        <AlertTitle>Trial details could not be loaded</AlertTitle>
        <AlertDescription className="flex flex-wrap items-center gap-2">
          <span>
            This tab needs the authoritative trial record before it can decide
            which stored resources exist.
          </span>
          <Button
            type="button"
            variant="secondary"
            size="sm"
            className="h-7"
            onClick={() => void revalidateTrial()}
            disabled={isValidatingTrialDetail}
          >
            {isValidatingTrialDetail ? "Retrying…" : "Retry"}
          </Button>
        </AlertDescription>
      </Alert>
    </div>
  );
  const trialStatusConfig = STATUS_CONFIG[trialStatus];
  const TrialStatusIcon = trialStatusConfig.icon;
  // Sum the navigable trials for this view (version-scoped in both callers),
  // not task.trials, which on the task page spans every version.
  const taskCost = sumTaskTrialCost(orderedTrials);
  const showQueueSnapshot =
    hasLiveQueueSnapshot(trial) && getQueueSnapshotItems(trial).length > 0;
  const sandboxBackend = getSandboxBackend(trial);
  const daytonaSandboxUrl =
    sandboxBackend?.id === "daytona" ? (sandboxBackend.href ?? null) : null;

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
    group.trials.some((groupTrial) => groupTrial.id === trial.id)
  );
  const currentGroup =
    currentGroupIndex >= 0 ? resolvedGroups[currentGroupIndex] : null;
  const currentGroupTrials = currentGroup?.trials ?? [];
  const currentGroupTrialIndex = currentGroupTrials.findIndex(
    (groupTrial) => groupTrial.id === trial.id
  );

  const navigateToGroupTrial = (groupIndex: number) => {
    if (!onNavigate || !currentGroup) return;
    const nextTrial = currentGroup.trials[groupIndex];
    if (!nextTrial) return;
    const nextIndex = orderedList.findIndex((item) => item.id === nextTrial.id);
    if (nextIndex < 0) return;
    onNavigate(nextTrial, nextIndex);
  };

  const handleOpenActiveQaTrial = (qaTrial: Trial) => {
    setActiveTab(
      isWorkerOwnedTrialStatus(qaTrial.status) || qaTrial.status === "retrying"
        ? "live"
        : "summary"
    );
    const index = orderedList.findIndex((item) => item.id === qaTrial.id);
    onNavigate?.(qaTrial, index >= 0 ? index : null);
  };

  const content = (
    <>
      <DrawerHeader className="border-border border-b px-4 py-3 sm:px-6 sm:py-4">
        <DrawerTitle className="flex min-w-0 items-center gap-2 pr-16 font-mono text-sm sm:text-base">
          <span className="min-w-0 truncate">{trial.name}</span>
          {(trial.kind ?? "agent") !== "agent" && (
            <span className="inline-flex shrink-0 items-center rounded-md border border-amber-500/40 bg-amber-500/10 px-1.5 py-0.5 font-mono text-[11px] font-medium text-amber-700 dark:text-amber-400">
              {trial.kind}
            </span>
          )}
          {showAnalysis && trial.task_version != null && (
            <span className="border-border bg-muted/50 text-muted-foreground inline-flex shrink-0 items-center rounded-md border px-1.5 py-0.5 font-mono text-[11px] font-medium">
              v{trial.task_version}
            </span>
          )}
          <span className="text-muted-foreground/50">·</span>
          <span className="text-muted-foreground flex min-w-0 items-center gap-1.5 leading-tight">
            <span className="flex min-w-0 flex-col items-center text-center leading-tight">
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
            {sandboxBackend && <SandboxBackendBadge backend={sandboxBackend} />}
          </span>
        </DrawerTitle>
        <DrawerDescription className="text-muted-foreground font-mono">
          <span className="truncate">{trial.id}</span>
        </DrawerDescription>
        <div className="text-muted-foreground flex flex-wrap items-stretch justify-between gap-2 pt-2 text-xs">
          <div className="flex items-center gap-1">
            {paneAction}
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
                    groupTrial.error_message
                  );
                  const groupConfig = STATUS_CONFIG[groupStatus];
                  const isPartial = groupStatus === "partial";
                  const partialLabel = isPartial
                    ? formatPartialRewardBadgeValue(groupTrial.reward)
                    : null;
                  const isActive = index === currentGroupTrialIndex;
                  return (
                    <Button
                      key={groupTrial.id}
                      type="button"
                      variant="ghost"
                      size="icon"
                      onClick={() => navigateToGroupTrial(index)}
                      className={cn(
                        "flex items-center justify-center p-0 leading-none transition hover:opacity-90",
                        STATUS_GLYPH_BOX,
                        groupConfig.matrixClass,
                        isPartial
                          ? "font-mono text-[9.5px] font-semibold tracking-[-0.02em] tabular-nums"
                          : "",
                        isActive
                          ? "ring-primary/60 ring-offset-background ring-2 ring-offset-1"
                          : ""
                      )}
                      style={getRewardStyle(groupTrial.reward)}
                      aria-label={`Trial ${index + 1} ${groupConfig.shortLabel}`}
                      title={`${groupConfig.shortLabel} • Trial ${index + 1}`}
                    >
                      {isPartial ? (
                        partialLabel
                      ) : (
                        <StatusIcon status={groupStatus} />
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
                OUTCOME_CARD_TONE[trialStatus]
              )}
              style={getRewardStyle(trial.reward, "panel")}
            >
              <CardContent className="px-2 py-1">
                <div className="flex items-center gap-1.5">
                  <TrialStatusIcon
                    className={cn(
                      "h-3.5 w-3.5 shrink-0",
                      trialStatus === "pass"
                        ? "text-emerald-500"
                        : trialStatus === "partial"
                          ? "text-amber-500"
                          : trialStatus === "fail"
                            ? "text-red-500"
                            : trialStatus === "harness-error"
                              ? "text-yellow-500"
                              : trialStatus === "queued"
                                ? "text-purple-500"
                                : trialStatus === "running"
                                  ? "text-blue-500"
                                  : trialStatus === "paused"
                                    ? "text-amber-500"
                                    : "text-gray-500",
                      (trialStatus === "pending" ||
                        trialStatus === "queued" ||
                        trialStatus === "running") &&
                        "animate-spin"
                    )}
                  />
                  <div className="min-w-0">
                    <div className="text-muted-foreground text-[8px] leading-none tracking-wider uppercase">
                      Reward
                    </div>
                    <div className="flex items-baseline gap-1">
                      <span className="font-mono text-sm leading-none font-bold">
                        {formatRewardValue(trial.reward)}
                      </span>
                      {trial.reward !== null && (
                        <span className="text-muted-foreground text-[9px] leading-none">
                          {formatRewardPercent(trial.reward)}
                        </span>
                      )}
                      <span className="text-muted-foreground text-[9px] leading-none capitalize">
                        {trialStatusConfig.shortLabel}
                      </span>
                    </div>
                  </div>
                </div>
              </CardContent>
            </Card>
            {(trial.cost_usd != null ||
              trial.input_tokens != null ||
              trial.output_tokens != null ||
              // A trial can be QA'd without the agent ever reporting a cost;
              // keep the card so its QA sidecar isn't hidden.
              hasDisplayableCostUsd(trial.qa_cost_usd)) && (
              <Card className="min-w-[120px] border">
                <CardContent className="flex h-full items-center px-2 py-1">
                  <div className="min-w-0">
                    <div className="text-muted-foreground text-[8px] leading-none tracking-wider uppercase">
                      Cost
                    </div>
                    <div className="mt-1 flex items-baseline gap-1">
                      <span className="font-mono text-sm leading-none font-bold tabular-nums">
                        {hasDisplayableCostUsd(trial.cost_usd) ? (
                          <>
                            {trial.cost_is_estimated ? "~" : ""}
                            {formatCostUsd(trial.cost_usd)}
                            <TrialNotRealSpendBadge
                              reason={trial.cost_exclusion_reason}
                            />
                          </>
                        ) : (
                          "—"
                        )}
                      </span>
                      {hasDisplayableCostUsd(trial.cost_usd) &&
                        taskCost.pricedCount > 1 &&
                        hasDisplayableCostUsd(taskCost.costUsd) &&
                        (() => {
                          const marks = costEstimateMarks(
                            taskCost.hasEstimated,
                            taskCost.hasNative
                          );
                          return (
                            <span className="text-muted-foreground text-[9px] leading-none">
                              of {marks.prefix}
                              {formatCostUsd(taskCost.costUsd)}
                              {marks.suffix} task
                            </span>
                          );
                        })()}
                      <QaCostSuffix
                        costUsd={trial.qa_cost_usd}
                        title="QA/analysis spend for this trial. Not included in the cost figure."
                      />
                    </div>
                    {(trial.input_tokens != null ||
                      trial.output_tokens != null) && (
                      <div className="text-muted-foreground mt-1 font-mono text-[9px] leading-none">
                        {formatTokenCount(
                          (trial.input_tokens ?? 0) + (trial.output_tokens ?? 0)
                        )}
                      </div>
                    )}
                  </div>
                </CardContent>
              </Card>
            )}
            {showRetry && (
              <Button
                onClick={handleRetry}
                disabled={!canRetry || retrying}
                title={actionsReady ? undefined : "Loading latest trial state."}
                variant="outline"
                size="sm"
                className="h-7 min-w-[128px] px-2 text-[10px] font-semibold tracking-wide uppercase"
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
            {daytonaSandboxUrl && (
              <Button
                asChild
                variant="outline"
                size="sm"
                className="h-7 min-w-[132px] px-2 text-[10px] font-semibold tracking-wide uppercase"
              >
                <a
                  href={daytonaSandboxUrl}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  <ExternalLink className="mr-1 h-3.5 w-3.5" />
                  Sandbox
                </a>
              </Button>
            )}
            {showDelete && (
              <Button
                onClick={() => {
                  if (!canDelete) return;
                  setDeleteError(null);
                  setDeleteDialogOpen(true);
                }}
                disabled={!canDelete || deleting}
                title={actionsReady ? undefined : "Loading latest trial state."}
                variant="outline"
                size="sm"
                className="text-destructive hover:bg-destructive/10 hover:text-destructive h-7 min-w-[112px] px-2 text-[10px] font-semibold tracking-wide uppercase"
              >
                {deleting ? (
                  <>
                    <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />
                    Deleting...
                  </>
                ) : (
                  <>
                    <Trash2 className="mr-1 h-3.5 w-3.5" />
                    Delete
                  </>
                )}
              </Button>
            )}
          </div>
        </div>
        {retryError && (
          <p className="pt-1 text-right text-xs text-red-500">{retryError}</p>
        )}
      </DrawerHeader>

      <Tabs
        value={effectiveTab}
        onValueChange={setActiveTab}
        className="flex flex-1 flex-col overflow-hidden"
      >
        <div className="border-border border-b px-4 sm:px-6">
          <TabsList className="h-10 gap-0 border-0 bg-transparent p-0 sm:h-12">
            <TabsTrigger
              value="summary"
              className="data-[state=active]:border-primary rounded-none px-3 text-xs data-[state=active]:border-b-2 data-[state=active]:bg-transparent sm:px-4 sm:text-sm"
            >
              <FileText className="mr-1 h-3.5 w-3.5 sm:mr-2 sm:h-4 sm:w-4" />
              Summary
            </TabsTrigger>
            {showLive && (
              <TabsTrigger
                value="live"
                className="data-[state=active]:border-primary rounded-none px-3 text-xs data-[state=active]:border-b-2 data-[state=active]:bg-transparent sm:px-4 sm:text-sm"
              >
                <Radio className="mr-1 h-3.5 w-3.5 text-red-500 sm:mr-2 sm:h-4 sm:w-4" />
                Live
              </TabsTrigger>
            )}
            <TabsTrigger
              value="files"
              className="data-[state=active]:border-primary rounded-none px-3 text-xs data-[state=active]:border-b-2 data-[state=active]:bg-transparent sm:px-4 sm:text-sm"
            >
              <FolderOpen className="mr-1 h-3.5 w-3.5 sm:mr-2 sm:h-4 sm:w-4" />
              Files
            </TabsTrigger>
            <TabsTrigger
              value="trajectory"
              className="data-[state=active]:border-primary rounded-none px-3 text-xs data-[state=active]:border-b-2 data-[state=active]:bg-transparent sm:px-4 sm:text-sm"
            >
              <Route className="mr-1 h-3.5 w-3.5 sm:mr-2 sm:h-4 sm:w-4" />
              Trajectory
            </TabsTrigger>
            <TabsTrigger
              value="artifacts"
              className="data-[state=active]:border-primary rounded-none px-3 text-xs data-[state=active]:border-b-2 data-[state=active]:bg-transparent sm:px-4 sm:text-sm"
            >
              <Package className="mr-1 h-3.5 w-3.5 sm:mr-2 sm:h-4 sm:w-4" />
              Artifacts
            </TabsTrigger>
          </TabsList>
        </div>

        <div className="flex-1 overflow-auto">
          <ActiveTabContent
            active={effectiveTab === "summary"}
            value="summary"
            className="m-0 p-4 sm:p-6"
          >
            <div className="space-y-4 pb-4">
              {showQueueSnapshot && (
                <Card className="border-purple-500/30 bg-purple-500/5">
                  <CardHeader className="px-4 pt-2 pb-1">
                    <CardTitle className="text-muted-foreground text-[11px] font-semibold tracking-wider uppercase">
                      Queue Snapshot
                    </CardTitle>
                  </CardHeader>
                  <CardContent className="px-4 pb-3">
                    <div className="flex flex-wrap gap-2">
                      {getQueueSnapshotItems(trial).map((item) => (
                        <span
                          key={item}
                          className="bg-background/60 text-foreground rounded border border-purple-500/20 px-2 py-1 font-mono text-[11px]"
                        >
                          {item}
                        </span>
                      ))}
                    </div>
                    <p className="text-muted-foreground mt-2 text-xs">
                      Live scheduler snapshot. This can move as other trials
                      start, finish, or get retried.
                    </p>
                  </CardContent>
                </Card>
              )}
              {verifierSummary && verifierSummary.tests > 0 && (
                <div className="text-muted-foreground flex items-center gap-1.5 px-4 py-1 text-[11px]">
                  {verifierSummary.failed > 0 ? (
                    <XCircle className="h-3.5 w-3.5 text-red-500" />
                  ) : (
                    <CheckCircle2 className="h-3.5 w-3.5 text-emerald-500" />
                  )}
                  <span className="text-foreground/80 font-mono tabular-nums">
                    {verifierSummary.passed}/{verifierSummary.tests}
                  </span>
                  tests passed
                </div>
              )}
              {/* Analysis card: self-updating; also hosts the run/re-run
                  button so analysis can be started even when it never ran. */}
              {showAnalysis && (
                <TrialAnalysisCard
                  key={trial.id}
                  trial={trial}
                  taskQaInProgress={taskQaInProgress}
                  apiBaseUrl={apiBaseUrl}
                  actionsReady={actionsReady}
                  activeQaTrial={activeQaTrial}
                  onFeedback={
                    feedbackExperimentId ? handleQaFeedback : undefined
                  }
                  onQueued={() => onRetry?.(task ? [task.id] : undefined)}
                  onOpenActiveQaTrial={
                    onNavigate ? handleOpenActiveQaTrial : undefined
                  }
                />
              )}

              {/* Execution Timeline - shows progress during running trials */}
              {trial.harbor_stage && (
                <Card>
                  <CardHeader className="px-4 pt-2 pb-1">
                    <CardTitle className="text-muted-foreground flex items-center justify-between text-[11px] font-semibold tracking-wider uppercase">
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

              {trial.harbor_sha && (
                <div className="text-muted-foreground px-4 py-1 text-[11px]">
                  Harbor:{" "}
                  <span className="font-mono">
                    {harborRepoLabel(trial.harbor_source)}@
                    {trial.harbor_sha.slice(0, 7)}
                  </span>
                </div>
              )}

              <TimingBreakdownBar
                createdAt={trial.created_at}
                startedAt={trial.started_at}
                finishedAt={trial.finished_at}
                compact
              />

              {/* Skip reason — a skipped trial carries its reason in
                  error_message, but it never ran, so render it as a neutral
                  note (CircleSlash + slate) rather than a red error card. */}
              {trial.error_message && trialStatus === "skipped" && (
                <Card className="border-slate-500/25 bg-slate-500/5">
                  <CardContent className="px-4 py-3">
                    <div className="flex items-start gap-2">
                      <CircleSlash className="mt-0.5 h-4 w-4 shrink-0 text-slate-500" />
                      <pre className="min-w-0 flex-1 font-mono text-sm wrap-break-word whitespace-pre-wrap text-slate-600 dark:text-slate-400">
                        {trial.error_message}
                      </pre>
                    </div>
                  </CardContent>
                </Card>
              )}

              {/* Error Card */}
              {trial.error_message && trialStatus !== "skipped" && (
                <Card className="border-red-500/30 bg-red-500/5">
                  <CardContent className="px-4 py-3">
                    <div className="flex items-start gap-2">
                      <AlertCircle className="mt-0.5 h-4 w-4 shrink-0 text-red-500" />
                      <div className="min-w-0 flex-1">
                        <pre className="font-mono text-sm wrap-break-word whitespace-pre-wrap text-red-600 dark:text-red-400">
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

              {/* Equivalent retry command — hidden from public viewers */}
              {showAnalysis && (
                <div>
                  <p className="text-muted-foreground mb-1 text-[11px]">
                    Equivalent retry command, reconstructed.
                  </p>
                  <CodeBlock
                    code={buildOddishRunCommand(trial, task)}
                    language="bash"
                    maxHeight="none"
                    className="opacity-60 transition-opacity hover:opacity-100"
                  />
                </div>
              )}
            </div>
          </ActiveTabContent>

          <ActiveTabContent
            active={effectiveTab === "live" && showLive}
            value="live"
            className="m-0 h-full p-0"
          >
            <LiveTranscriptPanel
              key={trial.id}
              trialId={trial.id}
              agent={trial.agent}
              apiBaseUrl={apiBaseUrl}
            />
          </ActiveTabContent>

          <ActiveTabContent
            active={effectiveTab === "files"}
            value="files"
            className="m-0 h-full p-0"
          >
            {trialDetailFailed ? (
              trialDetailErrorContent
            ) : (
              <TaskFilesPanel
                isOpen={isOpen}
                onClose={() => {}}
                activePane="file"
                taskId={null}
                filesUrl={`${apiBaseUrl}/trials/${trial.id}/files`}
                initialFilePath={filesTargetPath}
                selectedLines={selectedLines}
                onSelectLinesChange={setSelectedLines}
                onSelectedFileChange={handleSelectedFileChange}
                contentOnly
              />
            )}
          </ActiveTabContent>

          <ActiveTabContent
            active={effectiveTab === "artifacts"}
            value="artifacts"
            className="m-0 h-full p-0"
          >
            {trialDetailFailed ? (
              trialDetailErrorContent
            ) : (
              <ArtifactsViewer
                filesUrl={`${apiBaseUrl}/trials/${trial.id}/files`}
                trialId={trial.id}
                successfulAnalysisTrial={
                  trial.status === "success" &&
                  ["qa", "qa_eval", "audit", "summarize"].includes(
                    trial.kind ?? "agent"
                  )
                }
                initialFilePath={artifactsTargetPath}
                selectedLines={artifactsLines}
                onSelectLinesChange={setArtifactsLines}
                onSelectedFileChange={handleArtifactsFileChange}
              />
            )}
          </ActiveTabContent>

          <ActiveTabContent
            active={effectiveTab === "trajectory"}
            value="trajectory"
            className="m-0 h-full overflow-auto p-0"
          >
            {trialDetailFailed ? (
              trialDetailErrorContent
            ) : (
              <TrajectoryViewer
                trialId={trial.id}
                hasTrajectory={trial.has_trajectory}
                apiBaseUrl={apiBaseUrl}
                canRegenerateSummary={showAnalysis}
              />
            )}
          </ActiveTabContent>
        </div>
      </Tabs>
    </>
  );

  const deleteDialog = showDelete ? (
    <AlertDialog
      open={deleteDialogOpen}
      onOpenChange={(open) => {
        if (!open && !deleting) {
          setDeleteDialogOpen(false);
          setDeleteError(null);
        }
      }}
    >
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Delete this trial?</AlertDialogTitle>
          <AlertDialogDescription>
            This permanently deletes the trial, its logs, and any stored
            artifacts. In-flight runs for this trial will be cancelled. This
            action cannot be undone.
          </AlertDialogDescription>
        </AlertDialogHeader>
        {deleteError && (
          <Alert variant="destructive">
            <AlertTitle>Delete failed</AlertTitle>
            <AlertDescription>{deleteError}</AlertDescription>
          </Alert>
        )}
        <AlertDialogFooter>
          <AlertDialogCancel disabled={deleting}>Cancel</AlertDialogCancel>
          <AlertDialogAction
            onClick={(event) => {
              event.preventDefault();
              void handleDelete();
            }}
            disabled={!canDelete || deleting}
            className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
          >
            {deleting ? (
              <>
                <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />
                Deleting...
              </>
            ) : (
              "Delete trial"
            )}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  ) : null;

  if (contentOnly) {
    return (
      <div className="flex h-full flex-1 flex-col overflow-hidden">
        {content}
        {deleteDialog}
      </div>
    );
  }

  return (
    <>
      <ResizableDrawer
        open={isOpen}
        onOpenChange={(open) => !open && onClose()}
        defaultWidth={700}
        minWidth={420}
        maxWidth={900}
      >
        {content}
      </ResizableDrawer>
      {deleteDialog}
    </>
  );
}
