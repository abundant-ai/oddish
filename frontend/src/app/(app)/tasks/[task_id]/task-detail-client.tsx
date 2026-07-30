"use client";

import Link from "next/link";
import dynamic from "next/dynamic";
import { useCallback, useEffect, useMemo, useState } from "react";
import useSWR from "swr";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { TagEditor } from "@/components/tag-editor";
import { ChatButton } from "@/components/cc-chat/chat-button";
import { ProbeLaunchButton } from "@/components/probe-launch-button";
import { TaskPreTrialCard } from "@/components/task-pre-trial-card";
import { TaskProbeRunCard } from "@/components/task-probe-run-card";
import { TaskVerdictBadge } from "@/components/task-verdict-badge";
import { UnifiedDrawerWrapper } from "@/components/unified-drawer-wrapper";
import { ExperimentsList } from "@/components/experiments-list";
import { QaCostSuffix } from "@/components/qa-cost-suffix";
import { fetcher } from "@/lib/api";
import {
  buildExperimentAgentSummaries,
  getExperimentAgentKey,
  PROBE_AGENT_KEY,
} from "@/lib/experiment-agent-grouping";
import {
  formatCostUsd,
  formatDurationSec,
  formatTokenCount,
  hasDisplayableCostUsd,
  trialDurationSec,
} from "@/lib/format";
import {
  formatPartialRewardBadgeValue,
  formatRewardPercent,
  formatRewardValue,
  getMatrixStatus,
  getRewardStyle,
  STATUS_CONFIG,
} from "@/lib/status-config";
import { summarizeTrials, type TrialAggregate } from "@/lib/trial-aggregation";
import type {
  Task,
  TaskDetailResponse,
  TaskVersionSummary,
  Trial,
} from "@/lib/types";
import { formatRelativeTime, prBadge, taskPrUrl } from "@/lib/utils";
import {
  ArrowLeft,
  ChevronDown,
  ExternalLink,
  FileText,
  GitPullRequest,
  Loader2,
  Star,
} from "lucide-react";

const TaskFilesPanel = dynamic(
  () =>
    import("@/components/task-files-panel").then((mod) => mod.TaskFilesPanel),
  {
    ssr: false,
    loading: () => <DrawerContentLoading label="Loading task files..." />,
  }
);

const TrialDetailPanel = dynamic(
  () =>
    import("@/components/trial-detail-panel").then(
      (mod) => mod.TrialDetailPanel
    ),
  {
    ssr: false,
    loading: () => <DrawerContentLoading label="Loading trial details..." />,
  }
);

function DrawerContentLoading({ label }: { label: string }) {
  return (
    <div className="text-muted-foreground flex h-full min-h-[180px] items-center justify-center gap-2 text-sm">
      <Loader2 className="h-4 w-4 animate-spin" />
      <span>{label}</span>
    </div>
  );
}

function readVersionFromQuery(): string | null {
  if (typeof window === "undefined") return null;
  return new URLSearchParams(window.location.search).get("version");
}

function writeVersionToQuery(
  versionId: string | null,
  defaultId: string | null
) {
  if (typeof window === "undefined") return;
  const url = new URL(window.location.href);
  if (versionId == null || versionId === defaultId) {
    url.searchParams.delete("version");
  } else {
    url.searchParams.set("version", versionId);
  }
  window.history.replaceState(window.history.state, "", url.toString());
}

function CostBadge({
  cost,
  trialCount,
  hasEstimated,
  hasNative,
  size = "md",
}: {
  cost: number;
  trialCount: number;
  hasEstimated: boolean;
  hasNative: boolean;
  size?: "sm" | "md" | "lg";
}) {
  const valueClass =
    size === "lg"
      ? "text-[26px]"
      : size === "md"
        ? "text-[20px]"
        : "text-[13px]";
  const prefixClass =
    size === "lg"
      ? "text-[16px]"
      : size === "md"
        ? "text-[13px]"
        : "text-[10px]";
  const titleText =
    trialCount === 0
      ? "No cost data reported yet"
      : `Summed across ${trialCount} trial${trialCount === 1 ? "" : "s"}${
          hasEstimated && hasNative
            ? ". Mixed native + estimated values; ~ marks estimates."
            : hasEstimated
              ? ". Estimated from token counts × static model pricing."
              : ". Reported by the agent runtime."
        }`;

  // Sub-cent totals round to "$0.00", which reads as free; show the same dash
  // as "no data" rather than a zero the ledger doesn't mean.
  if (trialCount === 0 || !hasDisplayableCostUsd(cost)) {
    return (
      <span
        className={`font-display ${valueClass} leading-none tracking-[-0.02em] text-[color:var(--paper-ink-3)]`}
        title={titleText}
      >
        —
      </span>
    );
  }

  return (
    <span
      className={`font-display flex items-baseline gap-1 ${valueClass} leading-none font-medium tracking-[-0.02em] text-[color:var(--paper-ink)]`}
      title={titleText}
    >
      {hasEstimated && !hasNative && (
        <span
          className={`font-mono ${prefixClass} text-[color:var(--paper-ink-3)]`}
        >
          ~
        </span>
      )}
      {formatCostUsd(cost)}
      {hasEstimated && hasNative && (
        <span
          className={`font-mono ${prefixClass} text-[color:var(--paper-ink-3)]`}
        >
          *
        </span>
      )}
    </span>
  );
}

function KpiTile({
  label,
  children,
  hint,
  className = "",
}: {
  label: string;
  children: React.ReactNode;
  hint?: React.ReactNode;
  className?: string;
}) {
  return (
    <div
      className={`flex flex-col gap-1.5 border-r border-[color:var(--paper-line-2)] px-4 py-3 last:border-r-0 ${className}`}
    >
      <span className="font-mono text-[10px] font-semibold tracking-[0.09em] text-[color:var(--paper-ink-3)] uppercase">
        {label}
      </span>
      {children}
      {hint ? (
        <span className="font-mono text-[10px] text-[color:var(--paper-ink-3)]">
          {hint}
        </span>
      ) : null}
    </div>
  );
}

function TaskDetailHeader({
  task,
  onOpenTaskFiles,
  tagEditor,
}: {
  task: Task;
  onOpenTaskFiles: () => void;
  tagEditor?: React.ReactNode;
}) {
  return (
    <div className="flex flex-wrap items-end justify-between gap-x-6 gap-y-3">
      <div className="flex min-w-0 flex-1 flex-col gap-1">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h1 className="truncate font-mono text-[26px] leading-[1.25] font-semibold tracking-[-0.02em] text-[color:var(--paper-ink)]">
              {task.name}
            </h1>
            <Badge variant="outline" className="font-mono text-[11px]">
              v{task.current_version ?? "—"}
            </Badge>
            {tagEditor}
          </div>
        </div>
        {(() => {
          const affiliated = task.experiments?.length
            ? task.experiments
            : task.experiment_name
              ? [{ id: task.experiment_id, name: task.experiment_name }]
              : [];
          if (affiliated.length === 0) return null;
          return (
            <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 font-mono text-[11.5px] text-[color:var(--paper-ink-3)]">
              <span>
                {affiliated.length > 1 ? "experiments" : "experiment"}
              </span>
              <ExperimentsList
                experiments={affiliated}
                maxVisible={2}
                linkClassName="text-[color:var(--paper-ink-2)]"
              />
            </div>
          );
        })()}
        {(() => {
          const byline = task.github_username || task.user;
          if (!byline && !task.created_at) return null;
          return (
            <div className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-1 font-mono text-[11.5px] text-[color:var(--paper-ink-3)]">
              {byline ? <span>by {byline}</span> : null}
              {byline && task.created_at ? <span aria-hidden>·</span> : null}
              {task.created_at ? (
                <span title={new Date(task.created_at).toLocaleString()}>
                  created {formatRelativeTime(task.created_at)}
                </span>
              ) : null}
            </div>
          );
        })()}
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <ChatButton scopeKind="task" scopeId={task.name} />
        <ProbeLaunchButton
          taskId={task.id}
          taskName={task.name}
          variant="labeled"
          label="Launch probe"
          className="h-8 gap-1.5 rounded-[7px] border border-[color:var(--paper-line)] bg-[color:var(--paper-surface)] px-3 text-[12px]"
        />
        {(() => {
          const meta = task.github_meta;
          const prUrl = taskPrUrl(task.link, meta);
          if (!prUrl) return null;
          const { label, number } = prBadge(prUrl, meta?.pr_number);
          const title = meta?.pr_title;
          return (
            <a
              href={prUrl}
              target="_blank"
              rel="noopener noreferrer"
              title={
                title
                  ? `${title} — view on GitHub`
                  : "View pull request on GitHub"
              }
              className="hover:bg-accent inline-flex h-8 max-w-[200px] items-center justify-center gap-1.5 rounded-[7px] border border-[color:var(--paper-line)] bg-[color:var(--paper-surface)] px-3 text-[12px] transition-colors"
            >
              <GitPullRequest className="h-3.5 w-3.5 shrink-0" aria-hidden />
              <span className="min-w-0 truncate">
                {label}
                {number && (
                  <span className="text-muted-foreground"> #{number}</span>
                )}
              </span>
              <ExternalLink
                className="h-3 w-3 shrink-0 opacity-50"
                aria-hidden
              />
            </a>
          );
        })()}
        <Link href="/tasks">
          <Button
            type="button"
            variant="ghost"
            className="h-8 gap-1.5 rounded-[7px] border border-[color:var(--paper-line)] bg-[color:var(--paper-surface)] px-3 text-[12px]"
          >
            <ArrowLeft className="h-3.5 w-3.5" />
            All tasks
          </Button>
        </Link>
        <Button
          type="button"
          onClick={onOpenTaskFiles}
          className="h-8 gap-1.5 rounded-[7px] px-3 text-[12px]"
        >
          <FileText className="h-3.5 w-3.5" />
          View task files
        </Button>
      </div>
    </div>
  );
}

function summaryFromVersion(v: TaskVersionSummary): TrialAggregate {
  return {
    trialCount: v.trial_count,
    completed: v.completed_count,
    failed: v.failed_count,
    skipped: v.skipped_count,
    passCount: v.pass_count,
    partialCount: v.partial_count,
    failCount: v.fail_count,
    harnessErrorCount: 0,
    pendingCount: v.pending_count,
    rewardSum: v.reward_sum,
    rewardTotal: v.reward_total,
    costUsd: v.cost_usd,
    costTrialCount: v.cost_trial_count,
    costHasEstimated: v.cost_has_estimated,
    costHasNative: v.cost_has_native,
    // Task-scoped view: there is no owned-vs-gathered split, so owned == cost.
    ownedCostUsd: v.cost_usd,
    ownedTrialCount: v.cost_trial_count,
    ownedHasEstimated: v.cost_has_estimated,
    ownedHasNative: v.cost_has_native,
    tokenCount: 0,
    tokenTrialCount: 0,
    ownedTokenCount: 0,
    ownedTokenTrialCount: 0,
    billedCostUsd: v.billed_cost_usd,
    billedTrialCount: v.billed_trial_count,
    billedHasEstimated: v.billed_has_estimated,
    billedHasNative: v.billed_has_native,
    billedTokenCount: 0,
    billedTokenTrialCount: 0,
    lastRunAt: v.last_run_at ?? null,
  };
}

function VersionSwitcher({
  versions,
  selectedVersionId,
  onSelect,
}: {
  versions: TaskVersionSummary[];
  selectedVersionId: string | null;
  onSelect: (id: string) => void;
}) {
  if (versions.length === 0) return null;
  const selected = versions.find((v) => v.id === selectedVersionId);
  const triggerLabel = selected
    ? `v${selected.version}${selected.is_current ? " · default" : ""}`
    : "Select version";

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          type="button"
          variant="ghost"
          className="h-8 w-[220px] justify-between rounded-[7px] border border-[color:var(--paper-line)] bg-[color:var(--paper-surface)] px-3 font-mono text-[12px] text-[color:var(--paper-ink)] hover:bg-[color:var(--paper-surface-2)]"
        >
          <span className="truncate">{triggerLabel}</span>
          <ChevronDown className="ml-2 h-3.5 w-3.5 shrink-0 opacity-60" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent
        align="start"
        className="max-h-[min(60vh,var(--radix-dropdown-menu-content-available-height))] w-[320px] overflow-y-auto font-mono"
      >
        {versions.map((v) => {
          const label = v.is_current
            ? `v${v.version} · default`
            : `v${v.version}`;
          const cost =
            v.cost_trial_count > 0
              ? `${v.cost_has_estimated && !v.cost_has_native ? "~" : ""}${formatCostUsd(v.cost_usd)}`
              : "$0";
          const sub = `${v.trial_count} trial${v.trial_count === 1 ? "" : "s"} · ${cost}${v.message ? ` · ${v.message}` : ""}`;
          const isActive = v.id === selectedVersionId;
          return (
            <DropdownMenuItem
              key={v.id}
              onSelect={() => onSelect(v.id)}
              className={`flex flex-col items-start gap-0.5 px-3 py-2 ${
                isActive ? "bg-[color:var(--paper-surface-2)]" : ""
              }`}
            >
              <span className="font-mono text-[12px] font-semibold text-[color:var(--paper-ink)]">
                {label}
              </span>
              <span className="font-mono text-[10.5px] text-[color:var(--paper-ink-3)]">
                {sub}
              </span>
            </DropdownMenuItem>
          );
        })}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

function DefaultVersionControl({
  version,
  isSaving,
  onSetDefault,
}: {
  version: TaskVersionSummary | undefined;
  isSaving: boolean;
  onSetDefault: () => void;
}) {
  if (!version) return null;

  if (version.is_current) {
    return (
      <span
        className="inline-flex h-8 items-center gap-1.5 rounded-[7px] border border-amber-500/25 bg-amber-500/8 px-2.5 font-mono text-[10.5px] font-semibold text-amber-700 dark:text-amber-300"
        title="This version is shown by default and used for new runs"
      >
        <Star className="h-3 w-3 fill-current" />
        Default version
      </span>
    );
  }

  return (
    <TooltipProvider delayDuration={250}>
      <Tooltip>
        <TooltipTrigger asChild>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            disabled={isSaving}
            onClick={onSetDefault}
            className="h-8 gap-1.5 rounded-[7px] border border-[color:var(--paper-line)] bg-[color:var(--paper-surface)] px-2.5 font-mono text-[10.5px] font-semibold text-[color:var(--paper-ink-2)] hover:bg-[color:var(--paper-surface-2)] hover:text-[color:var(--paper-ink)]"
          >
            {isSaving ? (
              <Loader2 className="h-3 w-3 animate-spin" />
            ) : (
              <Star className="h-3 w-3" />
            )}
            {isSaving ? "Saving..." : "Make default"}
          </Button>
        </TooltipTrigger>
        <TooltipContent side="bottom" className="max-w-[280px]">
          Show v{version.version} by default on this task page and use it for
          new runs.
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}

function TrialChip({ trial, onClick }: { trial: Trial; onClick: () => void }) {
  const status = getMatrixStatus(
    trial.status,
    trial.reward,
    trial.error_message
  );
  const config = STATUS_CONFIG[status];
  const badgeLabel =
    status === "partial" ? formatPartialRewardBadgeValue(trial.reward) : null;

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button
          type="button"
          onClick={onClick}
          className={`flex h-[22px] w-[22px] items-center justify-center rounded-[4px] border font-mono leading-none font-semibold transition ${config.matrixClass} ${
            status === "partial"
              ? "text-[8px] tracking-[-0.03em]"
              : "text-[10px]"
          }`}
          style={getRewardStyle(trial.reward)}
          aria-label={`${trial.name} ${config.shortLabel}`}
        >
          {badgeLabel}
        </button>
      </TooltipTrigger>
      <TooltipContent>
        <div className="space-y-0.5">
          <div className="font-medium">{trial.name}</div>
          <div className="text-muted-foreground">{config.shortLabel}</div>
          {trial.reward !== null && (
            <div className="text-muted-foreground">
              Score {formatRewardValue(trial.reward)} (
              {formatRewardPercent(trial.reward)})
            </div>
          )}
          {trial.cost_usd != null && (
            <div className="text-muted-foreground">
              {trial.cost_is_estimated ? "~" : ""}
              {formatCostUsd(trial.cost_usd)}
            </div>
          )}
        </div>
      </TooltipContent>
    </Tooltip>
  );
}

function AgentCard({
  agentLabel,
  agent,
  model,
  trials,
  onTrialSelect,
}: {
  agentLabel: string;
  agent: string;
  model: string | null;
  trials: Trial[];
  onTrialSelect: (trial: Trial, trials: Trial[]) => void;
}) {
  const summary = useMemo(() => summarizeTrials(trials), [trials]);
  const scorePct =
    summary.rewardTotal > 0
      ? (summary.rewardSum / summary.rewardTotal) * 100
      : null;
  const avgCostUsd =
    summary.costTrialCount > 0
      ? summary.costUsd / summary.costTrialCount
      : null;
  const avgDurationSec = useMemo(() => {
    let sum = 0;
    let count = 0;
    for (const t of trials) {
      const d = trialDurationSec(t);
      if (d != null) {
        sum += d;
        count += 1;
      }
    }
    return count > 0 ? sum / count : null;
  }, [trials]);
  const sortedTrials = useMemo(
    () =>
      [...trials].sort((a, b) => {
        const aTime = a.finished_at || a.started_at || a.created_at;
        const bTime = b.finished_at || b.started_at || b.created_at;
        return aTime < bTime ? 1 : aTime > bTime ? -1 : 0;
      }),
    [trials]
  );

  return (
    <div className="rounded-[10px] border border-[color:var(--paper-line)] bg-[color:var(--paper-surface)]">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-[color:var(--paper-line-2)] px-4 py-3">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <span className="font-mono text-[14px] font-semibold text-[color:var(--paper-ink)]">
            {agent}
          </span>
          {model ? (
            <Badge variant="outline" className="font-mono text-[11px]">
              {model}
            </Badge>
          ) : null}
          {agentLabel !== agent && (
            <span className="font-mono text-[10px] text-[color:var(--paper-ink-3)]">
              {agentLabel}
            </span>
          )}
        </div>
        <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1 font-mono text-[11px] text-[color:var(--paper-ink-2)]">
          <span>
            <span className="text-[color:var(--paper-ink-3)]">trials</span>{" "}
            <span className="text-[color:var(--paper-ink)]">
              {summary.trialCount}
            </span>
          </span>
          <span>
            <span className="text-[color:var(--paper-ink-3)]">avg score</span>{" "}
            <span className="text-[color:var(--paper-ink)]">
              {scorePct != null
                ? `${scorePct.toFixed(0)}% (${summary.passCount}/${summary.rewardTotal})`
                : "—"}
            </span>
          </span>
          <span>
            <span className="text-[color:var(--paper-ink-3)]">total cost</span>{" "}
            <CostBadge
              cost={summary.costUsd}
              trialCount={summary.costTrialCount}
              hasEstimated={summary.costHasEstimated}
              hasNative={summary.costHasNative}
              size="sm"
            />
          </span>
          <span title="Mean cost per priced trial">
            <span className="text-[color:var(--paper-ink-3)]">avg cost</span>{" "}
            <span className="text-[color:var(--paper-ink)]">
              {hasDisplayableCostUsd(avgCostUsd)
                ? formatCostUsd(avgCostUsd)
                : "—"}
            </span>
          </span>
          <span title="Mean wall-clock duration (started_at → finished_at)">
            <span className="text-[color:var(--paper-ink-3)]">
              avg duration
            </span>{" "}
            <span className="text-[color:var(--paper-ink)]">
              {avgDurationSec != null ? formatDurationSec(avgDurationSec) : "—"}
            </span>
          </span>
          {summary.lastRunAt ? (
            <span title={new Date(summary.lastRunAt).toLocaleString()}>
              <span className="text-[color:var(--paper-ink-3)]">last run</span>{" "}
              <span className="text-[color:var(--paper-ink)]">
                {formatRelativeTime(summary.lastRunAt)}
              </span>
            </span>
          ) : null}
        </div>
      </div>
      <div className="px-4 py-3">
        <div className="flex flex-wrap gap-1.5">
          {sortedTrials.map((trial) => (
            <TrialChip
              key={trial.id}
              trial={trial}
              onClick={() => onTrialSelect(trial, sortedTrials)}
            />
          ))}
        </div>
      </div>
    </div>
  );
}

type DrawerState = {
  mode: "task" | "trial";
  trial: Trial | null;
  trialIndex: number | null;
  orderedTrials: Trial[];
  trialGroups: Array<{ agent: string; model: string | null; trials: Trial[] }>;
};

interface TaskDetailClientProps {
  taskId: string;
  initialDetail?: TaskDetailResponse | null;
  initialVersionId?: string | null;
}

export function TaskDetailClient({
  taskId,
  initialDetail,
  initialVersionId,
}: TaskDetailClientProps) {
  const swrKey = `/api/tasks/${encodeURIComponent(taskId)}/detail`;

  const { data, error, isLoading, mutate } = useSWR<TaskDetailResponse>(
    swrKey,
    fetcher,
    {
      refreshInterval: 30000,
      revalidateOnFocus: false,
      keepPreviousData: true,
      fallbackData: initialDetail ?? undefined,
    }
  );

  const detail = data ?? initialDetail ?? null;
  const task = detail?.task ?? null;
  const versions = useMemo(() => detail?.versions ?? [], [detail]);
  const totals = detail?.totals;

  const defaultVersionId = task?.current_version_id ?? versions[0]?.id ?? null;

  const [selectedVersionId, setSelectedVersionId] = useState<string | null>(
    () => initialVersionId ?? null
  );
  const [isSettingDefaultVersion, setIsSettingDefaultVersion] = useState(false);
  const [defaultVersionError, setDefaultVersionError] = useState<string | null>(
    null
  );

  useEffect(() => {
    if (
      selectedVersionId != null &&
      versions.some((v) => v.id === selectedVersionId)
    ) {
      return;
    }
    const fromUrl = readVersionFromQuery();
    if (fromUrl && versions.some((v) => v.id === fromUrl)) {
      setSelectedVersionId(fromUrl);
      return;
    }
    if (defaultVersionId != null) setSelectedVersionId(defaultVersionId);
  }, [versions, defaultVersionId, selectedVersionId]);

  const handleSelectVersion = useCallback(
    (id: string) => {
      setSelectedVersionId(id);
      setDefaultVersionError(null);
      writeVersionToQuery(id, defaultVersionId);
    },
    [defaultVersionId]
  );

  const trialsForVersion = useMemo(() => {
    if (!task?.trials || selectedVersionId == null) return [] as Trial[];
    return task.trials.filter((t) => t.task_version_id === selectedVersionId);
  }, [task?.trials, selectedVersionId]);

  const selectedVersion = versions.find((v) => v.id === selectedVersionId);
  const handleSetDefaultVersion = useCallback(async () => {
    if (!task || !selectedVersion || selectedVersion.is_current) return;

    const versionId = selectedVersion.id;
    const versionNumber = selectedVersion.version;
    setIsSettingDefaultVersion(true);
    setDefaultVersionError(null);
    try {
      const res = await fetch(
        `/api/tasks/${encodeURIComponent(task.id)}/versions/${versionNumber}/default`,
        { method: "PUT" }
      );
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(
          data.detail || data.error || "Failed to change the default version"
        );
      }

      await mutate(
        (current) =>
          current
            ? {
                ...current,
                task: {
                  ...current.task,
                  current_version_id: versionId,
                  current_version: versionNumber,
                },
                versions: current.versions.map((candidate) => ({
                  ...candidate,
                  is_current: candidate.id === versionId,
                })),
              }
            : current,
        { revalidate: false }
      );
      writeVersionToQuery(versionId, versionId);
      void mutate();
    } catch (err) {
      setDefaultVersionError(
        err instanceof Error
          ? err.message
          : "Failed to change the default version"
      );
    } finally {
      setIsSettingDefaultVersion(false);
    }
  }, [mutate, selectedVersion, task]);

  const versionSummary: TrialAggregate = useMemo(() => {
    if (selectedVersion) return summaryFromVersion(selectedVersion);
    return summarizeTrials(trialsForVersion);
  }, [selectedVersion, trialsForVersion]);
  const allVersionsSummary = useMemo(
    () => summarizeTrials(task?.trials ?? []),
    [task?.trials]
  );

  const tasksForGrouping = useMemo<Task[]>(
    () =>
      task
        ? [
            {
              ...task,
              trials: trialsForVersion,
            },
          ]
        : [],
    [task, trialsForVersion]
  );

  const { agentSummaries, modelScopedAgents } = useMemo(
    () => buildExperimentAgentSummaries(tasksForGrouping),
    [tasksForGrouping]
  );

  const realAgentCount = useMemo(
    () => agentSummaries.filter((s) => s.key !== PROBE_AGENT_KEY).length,
    [agentSummaries]
  );
  const realTrialCount = useMemo(
    () => trialsForVersion.filter((t) => !t.is_probe).length,
    [trialsForVersion]
  );

  const trialsByAgentKey = useMemo(() => {
    const map = new Map<string, Trial[]>();
    for (const trial of trialsForVersion) {
      const key = getExperimentAgentKey(trial, modelScopedAgents);
      const existing = map.get(key) ?? [];
      existing.push(trial);
      map.set(key, existing);
    }
    return map;
  }, [trialsForVersion, modelScopedAgents]);

  const trialGroups = useMemo(
    () =>
      agentSummaries.map((summary) => {
        const trials = trialsByAgentKey.get(summary.key) ?? [];
        return {
          agent: summary.key,
          model: summary.model,
          trials,
        };
      }),
    [agentSummaries, trialsByAgentKey]
  );

  const orderedTrials = useMemo(() => {
    const out: Trial[] = [];
    for (const group of trialGroups) out.push(...group.trials);
    return out;
  }, [trialGroups]);

  const [drawer, setDrawer] = useState<DrawerState | null>(null);
  const [drawerShowTask, setDrawerShowTask] = useState(true);
  const [drawerShowTrial, setDrawerShowTrial] = useState(true);

  const handleSelectTrial = useCallback(
    (trial: Trial) => {
      const trialIndex = orderedTrials.findIndex((t) => t.id === trial.id);
      setDrawer({
        mode: "trial",
        trial,
        trialIndex: trialIndex >= 0 ? trialIndex : null,
        orderedTrials,
        trialGroups,
      });
    },
    [orderedTrials, trialGroups]
  );

  const handleOpenTaskFiles = useCallback(() => {
    setDrawer({
      mode: "task",
      trial: null,
      trialIndex: null,
      orderedTrials,
      trialGroups,
    });
  }, [orderedTrials, trialGroups]);

  const handleNavigateToTrial = useCallback(
    (trial: Trial, trialIndex: number) => {
      setDrawer((prev) =>
        prev ? { ...prev, mode: "trial", trial, trialIndex } : prev
      );
    },
    []
  );

  const handleRerun = useCallback(() => {
    void mutate();
  }, [mutate]);

  const [isRunningJudge, setIsRunningJudge] = useState(false);
  const [isCancellingJudge, setIsCancellingJudge] = useState(false);
  const [judgeError, setJudgeError] = useState<string | null>(null);
  const [rerunningPreTrial, setRerunningPreTrial] = useState(false);
  const [preTrialError, setPreTrialError] = useState<string | null>(null);
  // Queue a fresh pre-trial audit of the task's CURRENT version. This is
  // the independent audit trigger: it does not classify trials and it
  // does not synthesize the verdict.
  const handleRerunPreTrial = useCallback(async () => {
    if (!task?.id || rerunningPreTrial) return;
    setRerunningPreTrial(true);
    setPreTrialError(null);
    try {
      const res = await fetch(`/api/tasks/${task.id}/qa/pre-trial`, {
        method: "POST",
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(
          data.detail || data.error || "Failed to queue pre-trial audit"
        );
      }
      // Await the refresh: the button must stay disabled until the card
      // shows the queued run, or a second click can queue another audit.
      await mutate();
    } catch (err) {
      setPreTrialError(
        err instanceof Error ? err.message : "Failed to queue pre-trial audit"
      );
    } finally {
      setRerunningPreTrial(false);
    }
  }, [task?.id, rerunningPreTrial, mutate]);
  const handleRunJudge = useCallback(async () => {
    if (!task?.id || isRunningJudge) return;
    setIsRunningJudge(true);
    setJudgeError(null);
    // One task-level QA job: classify every trial, then synthesize the
    // task verdict.
    try {
      const res = await fetch(`/api/tasks/${task.id}/qa/retry`, {
        method: "POST",
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || data.error || "Failed to queue QA");
      }
      void mutate();
    } catch (err) {
      setJudgeError(
        err instanceof Error ? err.message : "Failed to queue judge"
      );
    } finally {
      setIsRunningJudge(false);
    }
  }, [task?.id, isRunningJudge, mutate]);
  const handleCancelJudge = useCallback(async () => {
    if (!task?.id || isCancellingJudge) return;
    setIsCancellingJudge(true);
    setJudgeError(null);
    try {
      const res = await fetch(`/api/tasks/${task.id}/qa/cancel`, {
        method: "POST",
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || data.error || "Failed to cancel QA");
      }
      void mutate();
    } catch (err) {
      setJudgeError(err instanceof Error ? err.message : "Failed to cancel QA");
    } finally {
      setIsCancellingJudge(false);
    }
  }, [task, isCancellingJudge, mutate]);

  const versionScopedScorePct =
    versionSummary.rewardTotal > 0
      ? (versionSummary.rewardSum / versionSummary.rewardTotal) * 100
      : null;

  if (error && !detail) {
    return (
      <Alert variant="destructive">
        <AlertTitle>Failed to load task</AlertTitle>
        <AlertDescription>
          {error instanceof Error ? error.message : "Unknown error"}
        </AlertDescription>
      </Alert>
    );
  }

  if (!detail || !task) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-10 w-72" />
        <Skeleton className="h-20 w-full" />
        <Skeleton className="h-40 w-full" />
      </div>
    );
  }

  const versionLabel = selectedVersion
    ? `v${selectedVersion.version}${selectedVersion.is_current ? " · default" : ""}`
    : "Selected version";

  return (
    <TooltipProvider>
      <div className="space-y-4">
        <TaskDetailHeader
          task={task}
          onOpenTaskFiles={handleOpenTaskFiles}
          tagEditor={
            <TagEditor
              scope="TASK"
              targetId={task.id}
              taskId={task.id}
              initialTags={task.user_tags ?? []}
              onMutate={() => mutate()}
            />
          }
        />

        <div className="grid grid-cols-2 overflow-hidden rounded-[10px] border border-[color:var(--paper-line)] bg-[color:var(--paper-surface)] md:grid-cols-6">
          <KpiTile
            label="Total cost (all versions)"
            hint={
              totals && totals.cost_trial_count > 0
                ? `${totals.cost_trial_count} of ${totals.total_trials} trials priced`
                : totals && totals.total_trials > 0
                  ? `${totals.total_trials} trials, no cost data`
                  : "no trials yet"
            }
          >
            <span className="flex items-baseline gap-1.5">
              <CostBadge
                cost={totals?.cost_usd ?? 0}
                trialCount={totals?.cost_trial_count ?? 0}
                hasEstimated={totals?.cost_has_estimated ?? false}
                hasNative={totals?.cost_has_native ?? false}
                size="lg"
              />
              <QaCostSuffix
                costUsd={totals?.qa_cost_usd}
                size="tile"
                title="QA/analysis spend for this task's trials. Not included in the cost figure."
              />
            </span>
            {allVersionsSummary.tokenTrialCount > 0 && (
              <span className="font-mono text-[10px] text-[color:var(--paper-ink-3)]">
                {formatTokenCount(allVersionsSummary.tokenCount)}
              </span>
            )}
          </KpiTile>
          <KpiTile
            label="Billed spend"
            hint={
              totals && totals.billed_trial_count > 0
                ? `${totals.billed_trial_count} billed trial${
                    totals.billed_trial_count === 1 ? "" : "s"
                  }`
                : "no billed trials"
            }
          >
            <CostBadge
              cost={totals?.billed_cost_usd ?? 0}
              trialCount={totals?.billed_trial_count ?? 0}
              hasEstimated={totals?.billed_has_estimated ?? false}
              hasNative={totals?.billed_has_native ?? false}
              size="lg"
            />
          </KpiTile>
          <KpiTile
            label={`Spent on ${versionLabel}`}
            hint={
              versionSummary.costTrialCount > 0
                ? `${versionSummary.costTrialCount} trial${
                    versionSummary.costTrialCount === 1 ? "" : "s"
                  }`
                : "no cost data"
            }
          >
            <CostBadge
              cost={versionSummary.costUsd}
              trialCount={versionSummary.costTrialCount}
              hasEstimated={versionSummary.costHasEstimated}
              hasNative={versionSummary.costHasNative}
              size="lg"
            />
          </KpiTile>
          <KpiTile
            label="Trials"
            hint={`${versionSummary.completed} succeeded · ${versionSummary.failed} failed${
              versionSummary.skipped > 0
                ? ` · ${versionSummary.skipped} skipped`
                : ""
            }`}
          >
            <span className="font-display flex items-baseline gap-2 text-[26px] leading-none font-medium tracking-[-0.02em] text-[color:var(--paper-ink)]">
              {versionSummary.trialCount}
            </span>
          </KpiTile>
          <KpiTile
            label="Avg score"
            hint={
              versionSummary.rewardTotal > 0
                ? `${versionSummary.passCount} pass · ${versionSummary.partialCount} partial · ${versionSummary.failCount} fail`
                : "no scored trials"
            }
          >
            <span className="font-display flex items-baseline gap-2 text-[26px] leading-none font-medium tracking-[-0.02em] text-[color:var(--paper-ink)]">
              {versionScopedScorePct != null
                ? `${versionScopedScorePct.toFixed(1)}%`
                : "—"}
              {versionSummary.rewardTotal > 0 ? (
                <span
                  className="font-mono text-[12px] text-[color:var(--paper-ink-3)]"
                  title={`${versionSummary.passCount} of ${versionSummary.rewardTotal} scored trials passed (reward = 1)`}
                >
                  {versionSummary.passCount}/{versionSummary.rewardTotal} pass
                </span>
              ) : null}
            </span>
          </KpiTile>
          <KpiTile
            label="Last run"
            hint={
              versionSummary.lastRunAt
                ? new Date(versionSummary.lastRunAt).toLocaleString()
                : undefined
            }
          >
            <span className="font-display flex items-baseline gap-2 text-[20px] leading-none font-medium tracking-[-0.02em] text-[color:var(--paper-ink)]">
              {versionSummary.lastRunAt
                ? formatRelativeTime(versionSummary.lastRunAt)
                : "—"}
            </span>
          </KpiTile>
        </div>

        <div className="space-y-2">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-mono text-[10px] font-semibold tracking-[0.09em] text-[color:var(--paper-ink-3)] uppercase">
              Version
            </span>
            {isLoading ? (
              <Loader2 className="h-3 w-3 animate-spin text-[color:var(--paper-ink-3)]" />
            ) : null}
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <VersionSwitcher
              versions={versions}
              selectedVersionId={selectedVersionId}
              onSelect={handleSelectVersion}
            />
            {versions.length > 1 ? (
              <DefaultVersionControl
                version={selectedVersion}
                isSaving={isSettingDefaultVersion}
                onSetDefault={handleSetDefaultVersion}
              />
            ) : null}
            {selectedVersionId ? (
              <TagEditor
                key={selectedVersionId}
                scope="VERSION"
                targetId={selectedVersionId}
                taskId={task.id}
                initialTags={selectedVersion?.user_tags ?? []}
                onMutate={() => mutate()}
              />
            ) : null}
          </div>
          {defaultVersionError ? (
            <p
              role="alert"
              className="font-mono text-[10.5px] text-red-600 dark:text-red-400"
            >
              {defaultVersionError}
            </p>
          ) : null}
          {selectedVersion?.experiments?.length ? (
            <div
              className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 font-mono text-[11px] text-[color:var(--paper-ink-3)]"
              title="Experiments that ran trials against this version"
            >
              <span className="shrink-0">
                {selectedVersion.experiments.length > 1
                  ? "experiments"
                  : "experiment"}
              </span>
              <ExperimentsList
                experiments={selectedVersion.experiments}
                maxVisible={2}
                linkClassName="text-[color:var(--paper-ink-2)]"
              />
            </div>
          ) : null}
        </div>

        <TaskProbeRunCard
          taskId={task.id}
          versionId={selectedVersionId}
          headerSlot={
            <TaskVerdictBadge
              task={task}
              variant="inline"
              onRunJudge={handleRunJudge}
              onCancelJudge={handleCancelJudge}
              isRunning={isRunningJudge}
              isCancelling={isCancellingJudge}
              error={judgeError}
            />
          }
        />

        <TaskPreTrialCard
          findings={selectedVersion?.pre_trial_findings}
          status={selectedVersion?.pre_trial_status}
          error={selectedVersion?.pre_trial_error}
          costUsd={selectedVersion?.pre_trial_cost_usd}
          onRerun={handleRerunPreTrial}
          rerunning={rerunningPreTrial}
          queueError={preTrialError}
          isCurrentVersion={selectedVersion ? selectedVersion.is_current : true}
          taskQaActive={
            task?.verdict_status === "queued" ||
            task?.verdict_status === "running"
          }
        />

        <div className="space-y-3">
          <div className="flex items-baseline justify-between">
            <h2 className="font-mono text-[12px] font-semibold tracking-[0.06em] text-[color:var(--paper-ink-2)] uppercase">
              Agents
            </h2>
            <span className="font-mono text-[10.5px] text-[color:var(--paper-ink-3)]">
              {realAgentCount} agent
              {realAgentCount === 1 ? "" : "s"} · {realTrialCount} trial
              {realTrialCount === 1 ? "" : "s"}
            </span>
          </div>
          {agentSummaries.length === 0 ? (
            <div className="rounded-[10px] border border-dashed border-[color:var(--paper-line)] bg-[color:var(--paper-surface)] px-4 py-10 text-center text-[12px] text-[color:var(--paper-ink-3)]">
              No trials for this version yet.
            </div>
          ) : (
            agentSummaries.map((summary) => {
              const trials = trialsByAgentKey.get(summary.key) ?? [];
              return (
                <AgentCard
                  key={summary.key}
                  agentLabel={summary.label}
                  agent={summary.agent}
                  model={summary.model}
                  trials={trials}
                  onTrialSelect={handleSelectTrial}
                />
              );
            })
          )}
        </div>

        {drawer && (
          <UnifiedDrawerWrapper
            open={true}
            onOpenChange={(open) => !open && setDrawer(null)}
            mode={drawer.mode}
            showTask={drawerShowTask}
            showTrial={drawerShowTrial}
            onShowTaskChange={setDrawerShowTask}
            onShowTrialChange={setDrawerShowTrial}
            sideBySideLeft={
              <TaskFilesPanel
                isOpen={true}
                onClose={() => {}}
                taskId={null}
                probeTaskId={task.id}
                filesUrl={`/api/tasks/${task.id}/files`}
                apiBaseUrl="/api"
                contentOnly={true}
              />
            }
            taskContent={
              <TaskFilesPanel
                isOpen={true}
                onClose={() => setDrawer(null)}
                taskId={task.id}
                task={task}
                onRetryComplete={handleRerun}
                allowRetry={true}
                onNavigateToFirstTrial={
                  drawer.trialGroups.length > 0 &&
                  drawer.trialGroups[0].trials.length > 0
                    ? () => {
                        const firstTrial = drawer.trialGroups[0].trials[0];
                        handleSelectTrial(firstTrial);
                      }
                    : undefined
                }
                apiBaseUrl="/api"
                contentOnly={true}
              />
            }
            renderTrial={(paneAction) =>
              drawer.trial && (
                <TrialDetailPanel
                  isOpen={true}
                  onClose={() => setDrawer(null)}
                  trial={drawer.trial}
                  task={task}
                  orderedTrials={drawer.orderedTrials}
                  trialIndex={drawer.trialIndex}
                  trialGroups={drawer.trialGroups}
                  onNavigate={handleNavigateToTrial}
                  onNavigateToTask={() =>
                    setDrawer((prev) =>
                      prev
                        ? {
                            ...prev,
                            mode: "task",
                            trial: null,
                            trialIndex: null,
                          }
                        : prev
                    )
                  }
                  onRetry={handleRerun}
                  allowRetry={true}
                  apiBaseUrl="/api"
                  contentOnly={true}
                  paneAction={paneAction}
                />
              )
            }
          />
        )}
      </div>
    </TooltipProvider>
  );
}
