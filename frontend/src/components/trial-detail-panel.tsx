"use client";

import { useState, useEffect, useCallback, useMemo, useRef } from "react";
import dynamic from "next/dynamic";
import {
  PreTrialFindingList,
  preTrialAuditState,
} from "@/components/task-pre-trial-card";
import Image from "next/image";
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
  AlertTriangle,
  ExternalLink,
  Route,
  Package,
  Trash2,
  GitBranch,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { AnalysisProse } from "@/components/analysis-prose";
import { TimingBreakdownBar } from "@/components/timing-breakdown-bar";
import { CodeBlock } from "@/components/code-block";
import type { Trial, Task } from "@/lib/types";
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
import { LiveTranscriptPanel } from "@/components/live-transcript-panel";
import { QueueKeyIcon } from "@/components/queue-key-icon";
import { StatusIcon } from "@/components/status-icon";
import { useVerifierSummary } from "@/components/use-verifier-summary";
import { QaCostSuffix } from "@/components/qa-cost-suffix";

const TaskFilesPanel = dynamic(
  () =>
    import("@/components/task-files-panel").then((mod) => mod.TaskFilesPanel),
  {
    ssr: false,
    loading: () => <DrawerPanelLoading label="Loading files..." />,
  },
);

const ArtifactsViewer = dynamic(
  () =>
    import("@/components/artifacts-viewer").then((mod) => mod.ArtifactsViewer),
  {
    ssr: false,
    loading: () => <DrawerPanelLoading label="Loading artifacts..." />,
  },
);

const TrajectoryViewer = dynamic(
  () =>
    import("@/components/trajectory-viewer").then(
      (mod) => mod.TrajectoryViewer,
    ),
  {
    ssr: false,
    loading: () => <DrawerPanelLoading label="Loading trajectory..." />,
  },
);

const TrajectoryGraphView = dynamic(
  () =>
    import("@/components/trajectory-graph").then(
      (mod) => mod.TrajectoryGraphView,
    ),
  {
    ssr: false,
    loading: () => <DrawerPanelLoading label="Loading agent graph..." />,
  },
);


function DrawerPanelLoading({ label }: { label: string }) {
  return (
    <div className="text-muted-foreground flex h-full min-h-[160px] items-center justify-center gap-2 text-sm">
      <Loader2 className="h-4 w-4 animate-spin" />
      <span>{label}</span>
    </div>
  );
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
  onNavigate?: (trial: Trial, trialIndex: number) => void;
  onNavigateToTask?: () => void;
  onRetry?: (taskIds?: string[]) => void;
  onDelete?: (trial: Trial, task: Task | null) => Promise<void>;
  apiBaseUrl?: string;
  allowRetry?: boolean;
  /**
   * When false, the analysis card and run-analysis action are hidden
   * entirely — used by the public read-only share view.
   */
  showAnalysis?: boolean;
  allowDelete?: boolean;
  /** Render content only without ResizableDrawer wrapper */
  contentOnly?: boolean;
  /** Slot rendered alongside the navigation row — e.g. a "hide task" toggle. */
  paneAction?: React.ReactNode;
}

// Hardcoded feature flag: shows the per-trial "Re-run analysis" button on the
// QA card. Testing-only for now — flip to false to hide it without deleting
// the code (handleRerunAnalysis and /api/tasks/[task_id]/qa/backfill stay).
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
};

// The QA assessment card. It renders before any analysis exists, so the
// run button is reachable. It shows queued/running state with elapsed
// time. While analysis is active it polls the trial, so the result
// appears without reopening the drawer.
function TrialAnalysisCard({
  trial: trialProp,
  task,
  apiBaseUrl,
  onQueued,
}: {
  trial: Trial;
  task: Task | null;
  apiBaseUrl: string;
  onQueued?: () => void;
}) {
  const [live, setLive] = useState<Trial | null>(null);
  const [queuing, setQueuing] = useState(false);
  const [queueError, setQueueError] = useState<string | null>(null);
  // Set when WE queued a run. This keeps the card in its progress state
  // (and polling) across the gap before a worker claims the job.
  const [queuedAt, setQueuedAt] = useState<number | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const [logText, setLogText] = useState<string | null>(null);
  const [logOpen, setLogOpen] = useState(false);
  const [queuePosition, setQueuePosition] = useState<number | null>(null);
  const logRef = useRef<HTMLPreElement | null>(null);
  // Guards async completions: a response that lands after a trial switch
  // must not write another trial's state onto the open card.
  const trialIdRef = useRef(trialProp.id);
  // The parent passes a new onQueued function on every render. Reading it
  // through a ref keeps the poll interval alive across parent re-renders.
  const onQueuedRef = useRef(onQueued);
  useEffect(() => {
    onQueuedRef.current = onQueued;
  }, [onQueued]);

  // A different trial means the polled data no longer applies.
  useEffect(() => {
    trialIdRef.current = trialProp.id;
    setLive(null);
    setQueuing(false);
    setQueuedAt(null);
    setLogText(null);
    setLogOpen(false);
    setQueuePosition(null);
    setQueueError(null);
  }, [trialProp.id]);

  // The id check covers the first render after a trial switch, before the
  // reset effect above has cleared the previous trial's polled state.
  const trial = live && live.id === trialProp.id ? live : trialProp;
  const analysisActive =
    trial.analysis_status === "running" ||
    trial.analysis_status === "pending" ||
    trial.analysis_status === "queued";
  const waitingForWorker = queuedAt !== null && !analysisActive;
  const inProgress = analysisActive || waitingForWorker;

  // Poll for fresh trial state while analysis is in progress. The
  // cancelled flag drops in-flight responses on cleanup, so a late poll
  // cannot overlay another trial's analysis after navigation.
  useEffect(() => {
    if (!inProgress) return;
    let cancelled = false;
    const id = window.setInterval(async () => {
      try {
        const res = await fetch(`${apiBaseUrl}/trials/${trialProp.id}`, {
          cache: "no-store",
        });
        if (!res.ok || cancelled) return;
        const fresh = (await res.json()) as Trial;
        if (cancelled) return;
        setLive(fresh);
        // Terminal state ends the waiting phase. Refresh the parent too,
        // so the result survives closing the drawer or switching trials.
        if (
          fresh.analysis_status === "success" ||
          fresh.analysis_status === "failed"
        ) {
          setQueuedAt(null);
          onQueuedRef.current?.();
        }
      } catch {
        // Transient fetch error; the next tick retries.
      }
    }, 5000);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [inProgress, apiBaseUrl, trialProp.id]);

  // Fallback: if no worker picks the job up within 15 minutes, stop
  // showing progress and fall back to what the server says. Also drop the
  // optimistic `live` state — it holds the post-reset empty analysis, and
  // keeping it would shadow every later refresh from the parent.
  useEffect(() => {
    if (queuedAt === null) return;
    const id = window.setTimeout(() => {
      setQueuedAt(null);
      setLive(null);
    }, Math.max(0, queuedAt + 15 * 60_000 - Date.now()));
    return () => window.clearTimeout(id);
  }, [queuedAt]);

  // Tick the elapsed timer once a second while in progress.
  useEffect(() => {
    if (!inProgress) return;
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [inProgress]);

  // Load the analysis log and queue position: once on open, and on every
  // poll tick while the analysis is in progress. The cleanup always runs so
  // a response that lands after a trial switch cannot write stale state.
  useEffect(() => {
    let cancelled = false;
    const fetchLog = async () => {
      try {
        const res = await fetch(
          `${apiBaseUrl}/trials/${trialProp.id}/analysis-log`,
          { cache: "no-store" },
        );
        if (!res.ok || cancelled) return;
        const data = (await res.json()) as {
          log?: string | null;
          queue_position?: number | null;
        };
        if (!cancelled) {
          setLogText(data.log ?? null);
          setQueuePosition(data.queue_position ?? null);
        }
      } catch {
        // Transient fetch error; the next tick retries.
      }
    };
    void fetchLog();
    const id = inProgress ? window.setInterval(fetchLog, 5000) : null;
    return () => {
      cancelled = true;
      if (id !== null) window.clearInterval(id);
    };
  }, [apiBaseUrl, trialProp.id, inProgress]);

  // Keep the newest log lines in view while the analysis runs.
  useEffect(() => {
    const el = logRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [logText]);

  // Open the log panel when a run starts. The user can close and reopen it
  // at any time; renders never force it shut.
  useEffect(() => {
    if (inProgress) setLogOpen(true);
  }, [inProgress]);

  const hasAnalysis = Boolean(trial.analysis_status || trial.analysis) || inProgress;
  // Always SHOW the button when the feature is on. Disable it (with the
  // reason) when a run cannot be queued right now — a hidden button with
  // no explanation is impossible to debug from the UI. The trigger is
  // trial-level: only this trial's own active analysis blocks it.
  const showQueueButton = ENABLE_RERUN_ANALYSIS_BUTTON;
  // Mirrors _ANALYSIS_CLAIM_TTL_MINUTES: past the lease the backend treats
  // the worker as dead and allows a re-run, so the button must too.
  const runStale =
    trial.analysis_status === "running" &&
    trial.analysis_started_at != null &&
    now - new Date(trial.analysis_started_at).getTime() > 35 * 60_000;
  // Mirror the backend guards, so the button is disabled with the reason
  // instead of failing the request.
  const queueBlockedReason =
    inProgress && !runStale
      ? "Analysis is already running for this trial"
      : trial.status !== "success" && trial.status !== "failed"
        ? "The trial must finish before analysis can run"
        : null;

  if (!hasAnalysis && !showQueueButton) return null;

  const queueRun = async () => {
    if (queuing) return;
    const requestTrialId = trial.id;
    setQueuing(true);
    setQueueError(null);
    try {
      const res = await fetch(
        `${apiBaseUrl}/trials/${requestTrialId}/analysis/rerun`,
        { method: "POST" },
      );
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(
          data.detail || data.error || "Failed to queue analysis",
        );
      }
      if (trialIdRef.current !== requestTrialId) return;
      // Show progress immediately; polling takes over from here. The old
      // run's log is cleared server-side; clear it here too. Open the log
      // directly: a re-run over a stale RUNNING analysis keeps inProgress
      // true, so the open-on-start effect does not fire again.
      setQueuedAt(Date.now());
      setLogText(null);
      setLogOpen(true);
      setQueuePosition(null);
      setLive({
        ...trial,
        analysis_status: "queued",
        analysis: null,
        analysis_error: null,
        analysis_started_at: null,
      });
      onQueued?.();
    } catch (err) {
      if (trialIdRef.current === requestTrialId) {
        setQueueError(
          err instanceof Error ? err.message : "Failed to queue analysis",
        );
      }
    } finally {
      if (trialIdRef.current === requestTrialId) {
        setQueuing(false);
      }
    }
  };

  // Progress line: elapsed time while running, queue position while queued.
  // No narration — the log below shows what the analyzer is doing.
  let progressLine: string | null = null;
  if (inProgress) {
    if (trial.analysis_status === "running") {
      if (trial.analysis_started_at) {
        const secs = Math.max(
          0,
          Math.floor(
            (now - new Date(trial.analysis_started_at).getTime()) / 1000,
          ),
        );
        progressLine = `Running for ${Math.floor(secs / 60)}m ${secs % 60}s.`;
      }
    } else {
      progressLine =
        queuePosition !== null
          ? `Position ${queuePosition} in the QA queue.`
          : "Waiting for a QA worker.";
    }
  }

  return (
    <Card
      className={
        inProgress
          ? "border-blue-500/30 bg-blue-500/5"
          : trial.analysis?.classification?.startsWith("GOOD")
            ? "border-emerald-500/30 bg-emerald-500/5"
            : trial.analysis?.classification?.startsWith("BAD")
              ? "border-amber-500/30 bg-amber-500/5"
              : "border-slate-500/30 bg-slate-500/5"
      }
    >
      <CardContent className="px-4 py-3">
        <div className="text-muted-foreground mb-2 flex items-center justify-between text-[11px] font-semibold tracking-wider uppercase">
          <div>
            <span>QA Assessment</span>
            {trial.analysis?.prompt_version != null && (
              <span
                className="ml-2 normal-case font-normal tracking-normal"
                title={
                  trial.analysis.prompt_scope_id
                    ? `${trial.analysis.prompt_scope} override: ${trial.analysis.prompt_scope_id}`
                    : `${trial.analysis.prompt_scope ?? "global"} prompt`
                }
              >
                {trial.analysis.prompt_kind ?? "QA_POST_TRIAL"} v
                {trial.analysis.prompt_version} ·{" "}
                {trial.analysis.prompt_scope ?? "global"}
              </span>
            )}
          </div>
          {showQueueButton && (
            <button
              type="button"
              disabled={queuing || queueBlockedReason !== null}
              onClick={queueRun}
              className="text-muted-foreground hover:text-foreground rounded border px-1.5 py-0.5 text-[10px] font-medium normal-case tracking-normal disabled:cursor-not-allowed disabled:opacity-50"
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
          )}
        </div>
        {queueError && (
          <p className="mb-2 text-[11px] text-red-500">{queueError}</p>
        )}
        <div className="flex items-start gap-3">
          {inProgress ? (
            <Microscope className="mt-0.5 h-5 w-5 animate-pulse text-blue-500" />
          ) : trial.analysis?.classification?.startsWith("GOOD") ? (
            <CheckCircle2 className="mt-0.5 h-5 w-5 text-emerald-500" />
          ) : trial.analysis?.classification?.startsWith("BAD") ? (
            <AlertTriangle className="mt-0.5 h-5 w-5 text-amber-500" />
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
                    : "Analysis queued"}
                </span>
                <span className="text-muted-foreground text-xs">
                  {progressLine}
                </span>
              </div>
            ) : !hasAnalysis ? (
              <div className="flex flex-col gap-1">
                <span className="font-mono text-sm font-bold">
                  No analysis yet
                </span>
                <span className="text-muted-foreground text-xs">
                  This trial has not been analyzed.
                </span>
              </div>
            ) : (
              <>
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-mono text-sm font-bold">
                    {trial.analysis?.classification?.replace("_", " ") ||
                      "Analysis"}
                  </span>
                  {trial.analysis?.subtype &&
                    !/^n\/a/i.test(trial.analysis.subtype) && (
                      <span
                        className={cn(
                          "rounded border px-1.5 py-0.5 font-mono text-[10px]",
                          trial.analysis?.classification?.startsWith("BAD")
                            ? "border-amber-500/50 text-amber-600 dark:text-amber-400"
                            : trial.analysis?.classification?.startsWith("GOOD")
                              ? "border-emerald-500/50 text-emerald-600 dark:text-emerald-400"
                              : "border-border text-muted-foreground",
                        )}
                      >
                        {trial.analysis.subtype.replace(/_/g, " ")}
                      </span>
                    )}
                </div>
                {trial.analysis_status === "failed" &&
                  trial.analysis_error && (
                    <p className="mt-1 text-xs text-red-500">
                      Analysis failed: {trial.analysis_error}
                    </p>
                  )}
                {trial.analysis?.root_cause &&
                  trial.analysis.root_cause !== trial.analysis.evidence && (
                    <div className="border-border/60 bg-muted/30 mt-3 rounded-md border p-2.5">
                      <span className="text-foreground/80 font-mono text-[10px] font-semibold tracking-wider uppercase">
                        Root cause
                      </span>
                      <AnalysisProse
                        text={trial.analysis.root_cause}
                        className="text-muted-foreground mt-1"
                      />
                    </div>
                  )}
                {trial.analysis?.recommendation &&
                  !/^n\/a/i.test(trial.analysis.recommendation) && (
                    <div className="border-border/60 bg-muted/30 mt-2 rounded-md border border-l-2 border-l-amber-500/60 p-2.5">
                      <span className="text-foreground/80 font-mono text-[10px] font-semibold tracking-wider uppercase">
                        Fix
                      </span>
                      <AnalysisProse
                        text={trial.analysis.recommendation}
                        className="text-muted-foreground mt-1"
                      />
                    </div>
                  )}
                {(trial.analysis?.action_items?.length ?? 0) > 0 && (
                  <div className="mt-3">
                    <span className="text-foreground/80 font-mono text-[10px] font-semibold tracking-wider uppercase">
                      Action items ({trial.analysis!.action_items!.length})
                    </span>
                    <div className="mt-1">
                      <PreTrialFindingList
                        items={trial.analysis!.action_items!}
                      />
                    </div>
                  </div>
                )}
                {trial.analysis?.evidence &&
                  (trial.analysis.root_cause &&
                  trial.analysis.root_cause !== trial.analysis.evidence ? (
                    <details className="mt-2">
                      <summary className="text-muted-foreground cursor-pointer text-[11px] font-medium select-none">
                        Evidence
                      </summary>
                      <AnalysisProse
                        text={trial.analysis.evidence}
                        className="text-muted-foreground/90 mt-1"
                      />
                    </details>
                  ) : (
                    <AnalysisProse
                      text={trial.analysis.evidence}
                      className="text-muted-foreground/90 mt-2"
                    />
                  ))}
              </>
            )}
          </div>
        </div>
        {logText && (
          <details
            className="mt-3"
            open={logOpen}
            onToggle={(event) =>
              setLogOpen((event.target as HTMLDetailsElement).open)
            }
          >
            <summary className="text-muted-foreground cursor-pointer text-[11px] font-medium select-none">
              Analysis log
            </summary>
            <pre
              ref={logRef}
              className="bg-muted/40 mt-1 max-h-48 overflow-auto rounded p-2 font-mono text-[10.5px] leading-relaxed whitespace-pre-wrap"
            >
              {logText}
            </pre>
          </details>
        )}
      </CardContent>
    </Card>
  );
}

function buildOddishRunCommand(trial: Trial, task: Task): string {
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
    const modelArg =
      trial.provider && !trial.model.includes("/")
        ? `${trial.provider}/${trial.model}`
        : trial.model;
    parts.push(`-m ${modelArg}`);
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
  return ["queued", "retrying", "running", "pending"].includes(trial.status);
}

type SandboxBackendId = "daytona" | "modal";

type SandboxBackend = {
  id: SandboxBackendId;
  label: string;
  logoSrc: string;
  logoWidth: number;
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
};

function normalizeSandboxBackend(
  provider: string | null | undefined,
): SandboxBackendId | null {
  const normalized = provider?.trim().toLowerCase();
  if (normalized === "daytona" || normalized === "modal") {
    return normalized;
  }
  return null;
}

function getSandboxBackend(trial: Trial): SandboxBackend | null {
  const trialBackendId = normalizeSandboxBackend(trial.environment);
  const sandboxJob = trial.jobs?.find((job) =>
    Boolean(normalizeSandboxBackend(job.provider)),
  );
  const backendId =
    trialBackendId ?? normalizeSandboxBackend(sandboxJob?.provider);
  if (!backendId) return null;

  const backend = SANDBOX_BACKENDS[backendId];
  if (backendId === "daytona" && sandboxJob?.external_id) {
    return {
      ...backend,
      href: `https://app.daytona.io/dashboard/sandboxes?sandboxId=${encodeURIComponent(
        sandboxJob.external_id,
      )}`,
    };
  }

  return backend;
}

function SandboxBackendBadge({ backend }: { backend: SandboxBackend }) {
  const content = (
    <>
      <span className="inline-flex h-4 items-center justify-center rounded-sm bg-white px-1">
        <Image
          src={backend.logoSrc}
          alt={`${backend.label} logo`}
          width={backend.logoWidth}
          height={10}
          className="h-2.5 w-auto object-contain"
        />
      </span>
      {backend.id === "modal" && (
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
  trial,
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
  allowDelete = false,
  contentOnly = false,
  paneAction,
}: TrialDetailPanelProps) {
  const searchParams = useSearchParams();

  const verifierSummary = useVerifierSummary(trial, apiBaseUrl, isOpen);

  const validTabs = useMemo(
    () =>
      new Set([
        "summary",
        "live",
        "files",
        "trajectory",
        // Only a valid target when the Agent Graph tab is actually rendered;
        // otherwise a ?tab=agent-graph link (e.g. public share) would select a
        // panel that doesn't exist and leave the drawer body blank.
        ...(showAnalysis ? ["agent-graph"] : []),
        "artifacts",
      ]),
    [showAnalysis],
  );

  const [activeTab, setActiveTab] = useState(() => {
    const urlTab = searchParams.get("tab");
    return urlTab && validTabs.has(urlTab) ? urlTab : "summary";
  });
  const [showFullError, setShowFullError] = useState(false);
  const [retrying, setRetrying] = useState(false);
  const [retryError, setRetryError] = useState<string | null>(null);
  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
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
  const canDelete = allowDelete && Boolean(onDelete) && Boolean(trial);
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

  const handleDelete = async () => {
    if (!trial || !onDelete || deleting) return;
    setDeleting(true);
    setDeleteError(null);

    try {
      await onDelete(trial, task);
      setDeleteDialogOpen(false);
      onClose();
    } catch (err) {
      setDeleteError(
        err instanceof Error ? err.message : "Failed to delete trial",
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
    setFilesTargetPath(filePath);
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
  const showLive =
    showAnalysis && (trial.status === "running" || trial.status === "retrying");
  const effectiveTab =
    activeTab === "live" && !showLive ? "summary" : activeTab;
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
      <DrawerHeader className="border-border border-b px-4 py-3 sm:px-6 sm:py-4">
        <DrawerTitle className="flex min-w-0 items-center gap-2 pr-8 font-mono text-sm sm:text-base">
          <span className="min-w-0 truncate">{trial.name}</span>
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
                    groupTrial.error_message,
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
                          : "",
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
                OUTCOME_CARD_TONE[trialStatus],
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
                                  : "text-gray-500",
                      (trialStatus === "pending" ||
                        trialStatus === "queued" ||
                        trialStatus === "running") &&
                        "animate-spin",
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
                            taskCost.hasNative,
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
                          (trial.input_tokens ?? 0) +
                            (trial.output_tokens ?? 0),
                        )}
                      </div>
                    )}
                  </div>
                </CardContent>
              </Card>
            )}
            {canRetry && (
              <Button
                onClick={handleRetry}
                disabled={retrying}
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
            {canDelete && (
              <Button
                onClick={() => {
                  setDeleteError(null);
                  setDeleteDialogOpen(true);
                }}
                disabled={deleting}
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
            {showAnalysis && (
              <TabsTrigger
                value="agent-graph"
                className="data-[state=active]:border-primary rounded-none px-3 text-xs data-[state=active]:border-b-2 data-[state=active]:bg-transparent sm:px-4 sm:text-sm"
              >
                <GitBranch className="mr-1 h-3.5 w-3.5 sm:mr-2 sm:h-4 sm:w-4" />
                Agent Graph
              </TabsTrigger>
            )}
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
          <TabsContent value="summary" className="m-0 p-4 sm:p-6">
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
                  trial={trial}
                  task={task}
                  apiBaseUrl={apiBaseUrl}
                  onQueued={() => onRetry?.(task ? [task.id] : undefined)}
                />
              )}

              {/* Pre-trial audit of the version THIS trial ran on. Sits below
                  the post-trial assessment: what the source was known to be
                  wrong with, next to what the agent then did with it. */}
              {(() => {
                const items = trial.pre_trial_findings ?? [];
                const state = preTrialAuditState(
                  trial.pre_trial_status,
                  items.length
                );
                if (state === "unaudited") return null;
                return (
                  <Card className="border-slate-500/25 bg-slate-500/5">
                    <CardContent className="px-4 py-3">
                      <div className="text-muted-foreground mb-2 flex items-baseline justify-between text-[11px] font-semibold tracking-wider uppercase">
                        <span>Pre-trial audit</span>
                        <span className="normal-case font-normal tracking-normal">
                          {state === "running"
                            ? "running…"
                            : state === "failed"
                              ? "failed"
                              : state === "clean"
                                ? "no defects found"
                                : `${items.length} finding${items.length === 1 ? "" : "s"}`}
                          {hasDisplayableCostUsd(trial.pre_trial_cost_usd)
                            ? ` · ${formatCostUsd(trial.pre_trial_cost_usd)}`
                            : ""}
                        </span>
                      </div>
                      {state === "failed" && trial.pre_trial_error ? (
                        <p className="text-muted-foreground font-mono text-[11px] break-all">
                          {trial.pre_trial_error}
                        </p>
                      ) : null}
                      {items.length > 0 ? (
                        <div className="space-y-2">
                          <PreTrialFindingList items={items} />
                        </div>
                      ) : null}
                    </CardContent>
                  </Card>
                );
              })()}

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

              {/* Discreet reproduction command — hidden from public viewers */}
              {showAnalysis && (
                <CodeBlock
                  code={buildOddishRunCommand(trial, task)}
                  language="bash"
                  maxHeight="none"
                  className="opacity-60 transition-opacity hover:opacity-100"
                />
              )}
            </div>
          </TabsContent>

          {showLive && (
            <TabsContent value="live" className="m-0 h-full p-0">
              <LiveTranscriptPanel
                key={trial.id}
                trialId={trial.id}
                agent={trial.agent}
                apiBaseUrl={apiBaseUrl}
              />
            </TabsContent>
          )}

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

          <TabsContent value="artifacts" className="m-0 h-full p-0">
            <ArtifactsViewer
              filesUrl={`${apiBaseUrl}/trials/${trial.id}/files`}
            />
          </TabsContent>

          <TabsContent
            value="trajectory"
            className="m-0 h-full overflow-auto p-0"
          >
            <TrajectoryViewer
              trialId={trial.id}
              hasTrajectory={trial.has_trajectory}
              apiBaseUrl={apiBaseUrl}
            />
          </TabsContent>

          {showAnalysis && (
            <TabsContent
              value="agent-graph"
              className="m-0 h-full overflow-auto p-0"
            >
              <TrajectoryGraphView
                trialId={trial.id}
                hasTrajectory={trial.has_trajectory}
                apiBaseUrl={apiBaseUrl}
              />
            </TabsContent>
          )}
        </div>
      </Tabs>
    </>
  );

  const deleteDialog = canDelete ? (
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
            disabled={deleting}
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
