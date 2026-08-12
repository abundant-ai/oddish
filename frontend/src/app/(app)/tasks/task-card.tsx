"use client";

import Link from "next/link";
import { ExternalLink, GitPullRequest } from "lucide-react";
import { useSWRConfig } from "swr";
import { Badge, badgeVariants } from "@/components/ui/badge";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { ExperimentsList } from "@/components/experiments-list";
import { QaCostSuffix } from "@/components/qa-cost-suffix";
import { TagChip } from "@/components/tag-chip";
import { isBaselineAgentName } from "@/lib/experiment-agent-grouping";
import { formatCostUsd, hasDisplayableCostUsd } from "@/lib/format";
import {
  formatPartialRewardBadgeValue,
  formatRewardPercent,
  formatRewardValue,
  getMatrixStatus,
  getRewardStyle,
  STATUS_CONFIG,
} from "@/lib/status-config";
import type { TaskBrowseItem } from "@/lib/types";
import {
  isBrowseTaskDetail,
  taskDetailFromBrowse,
  taskDetailKey,
  type TaskDetailResource,
} from "@/lib/task-detail-resource";
import {
  cn,
  formatRelativeTime,
  formatShortDateTime,
  prBadge,
  taskPrUrl,
} from "@/lib/utils";
import { useSelection } from "./selection-context";

function ExperimentsCell({ task }: { task: TaskBrowseItem }) {
  if (task.experiments.length === 0) {
    return <span className="text-muted-foreground">—</span>;
  }

  return (
    <ExperimentsList
      experiments={task.experiments}
      maxVisible={3}
      layout="stacked"
      className="text-muted-foreground text-xs"
      linkClassName="text-[#5d77a5] transition-colors hover:text-[#526a95] dark:text-[#a8b8d2] dark:hover:text-[#c0cde1]"
    />
  );
}

function getLatestTrialStatusCounts(task: TaskBrowseItem) {
  return {
    pass: task.pass_count,
    partial: task.partial_count,
    fail: task.fail_count,
    "harness-error": task.harness_count,
    skipped: task.skipped_count,
    pending: task.pending_count,
  };
}

function PassRateCell({ task }: { task: TaskBrowseItem }) {
  const rewardSum = task.reward_sum ?? task.reward_success;
  const hasScore = task.reward_total > 0;
  const avgScore = hasScore
    ? Math.round((rewardSum / task.reward_total) * 100)
    : null;
  const toneClass =
    avgScore == null
      ? "text-muted-foreground"
      : avgScore >= 80
        ? "text-[#5c8e43] dark:text-[#85b85c]"
        : avgScore >= 35
          ? "text-yellow-400"
          : "text-rose-400";
  const statusCounts = getLatestTrialStatusCounts(task);
  const summaryItems = [
    { key: "pass", label: "Pass", count: statusCounts.pass },
    { key: "partial", label: "Partial", count: statusCounts.partial },
    { key: "fail", label: "Fail", count: statusCounts.fail },
    {
      key: "harness-error",
      label: "Harness",
      count: statusCounts["harness-error"],
    },
    // Skipped is its own bucket (like Harness): a non-pass in the rate, shown
    // separately. Rendered only when present so it doesn't clutter every card.
    { key: "skipped", label: "Skipped", count: statusCounts.skipped },
    {
      key: "pending",
      label: "Pending",
      count: statusCounts.pending,
    },
  ] as const;

  return (
    <div className="space-y-1.5">
      <div className="flex items-baseline justify-between gap-3">
        <div className={`text-base leading-none font-medium ${toneClass}`}>
          {avgScore == null ? "—" : `${avgScore}%`}
        </div>
        <div className="text-muted-foreground text-[11px] leading-none">
          {hasScore
            ? `${rewardSum.toFixed(2)}/${task.reward_total}`
            : "No completed trials"}
        </div>
      </div>
      {task.latest_trials.length > 0 ? (
        <div className="text-muted-foreground flex flex-wrap gap-x-2.5 gap-y-0.5 text-[10px] leading-none">
          {summaryItems
            .filter((item) => item.key !== "skipped" || item.count > 0)
            .map((item) => {
              const config = STATUS_CONFIG[item.key];
              return (
                <div
                  key={item.key}
                  className="flex items-center gap-1 whitespace-nowrap"
                >
                  <span
                    className={`inline-flex h-2 w-2 rounded-full ${config.bracketClass}`}
                  />
                  <span>{item.label}</span>
                  <span className="text-foreground font-mono">
                    {item.count}
                  </span>
                </div>
              );
            })}
        </div>
      ) : (
        <div className="text-muted-foreground text-[10px] leading-none">
          No latest-version trials
        </div>
      )}
    </div>
  );
}

type LatestTrial = TaskBrowseItem["latest_trials"][number];

function TrialIcon({ trial }: { trial: LatestTrial }) {
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
        <div
          className={`flex h-[18px] w-[18px] items-center justify-center rounded-[4px] border font-mono leading-none font-semibold ${config.matrixClass} ${status === "partial" ? "text-[7px] tracking-[-0.03em]" : ""}`}
          style={getRewardStyle(trial.reward)}
          aria-label={`${trial.name} ${config.shortLabel}`}
        >
          {badgeLabel}
        </div>
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
        </div>
      </TooltipContent>
    </Tooltip>
  );
}

function trialModelKey(model: string | null): string {
  return model?.trim() || "default";
}

type TrialGroup = {
  key: string;
  agent: string;
  // Badge text: the distinguishing model for a model-scoped agent ("default"
  // for a null/blank model), the single real model otherwise, else null.
  modelLabel: string | null;
  trials: LatestTrial[];
};

// Mirrors the interior task view's agent grouping (experiment-agent-grouping):
// one section per agent, split by model only when an agent ran more than one.
function groupLatestTrials(trials: LatestTrial[]): TrialGroup[] {
  const modelsByAgent = new Map<string, Set<string>>();
  for (const trial of trials) {
    const models = modelsByAgent.get(trial.agent) ?? new Set<string>();
    models.add(trialModelKey(trial.model));
    modelsByAgent.set(trial.agent, models);
  }
  const modelScoped = new Set(
    Array.from(modelsByAgent.entries())
      .filter(([, models]) => models.size > 1)
      .map(([agent]) => agent)
  );

  const groups = new Map<string, TrialGroup>();
  for (const trial of trials) {
    const scoped = modelScoped.has(trial.agent);
    const key = scoped
      ? `${trial.agent}/${trialModelKey(trial.model)}`
      : trial.agent;
    const group = groups.get(key);
    if (group) {
      group.trials.push(trial);
    } else {
      groups.set(key, {
        key,
        agent: trial.agent,
        modelLabel: scoped
          ? trialModelKey(trial.model)
          : trial.model?.trim() || null,
        trials: [trial],
      });
    }
  }

  // Real agents before baselines (nop/oracle), then most-run first.
  return Array.from(groups.values()).sort((a, b) => {
    const baselineDelta =
      Number(isBaselineAgentName(a.agent)) -
      Number(isBaselineAgentName(b.agent));
    if (baselineDelta !== 0) return baselineDelta;
    return b.trials.length - a.trials.length;
  });
}

function groupScorePct(trials: LatestTrial[]): number | null {
  const rewards = trials.flatMap((t) => (t.reward === null ? [] : [t.reward]));
  if (rewards.length === 0) return null;
  return (rewards.reduce((sum, r) => sum + r, 0) / rewards.length) * 100;
}

function TrialGraphics({ task }: { task: TaskBrowseItem }) {
  if (task.latest_trials.length === 0) {
    return (
      <div className="border-border/70 text-muted-foreground rounded-md border border-dashed px-3 py-3 text-center text-xs">
        No latest-version trials yet.
      </div>
    );
  }

  const groups = groupLatestTrials(task.latest_trials);

  return (
    <div className="space-y-2">
      {groups.map((group) => {
        const scorePct = groupScorePct(group.trials);
        return (
          <div key={group.key} className="space-y-1">
            <div className="flex flex-wrap items-center justify-between gap-x-2 gap-y-0.5">
              <div className="flex min-w-0 flex-wrap items-center gap-1.5">
                <span className="text-foreground font-mono text-[11px] font-medium">
                  {group.agent}
                </span>
                {group.modelLabel ? (
                  <Badge
                    variant="outline"
                    className="w-fit font-mono text-[10px]"
                  >
                    {group.modelLabel}
                  </Badge>
                ) : null}
              </div>
              <div className="text-muted-foreground flex items-center gap-2 font-mono text-[10px] leading-none">
                <span>
                  {group.trials.length} trial
                  {group.trials.length === 1 ? "" : "s"}
                </span>
                <span className="text-foreground">
                  {scorePct == null ? "—" : `${scorePct.toFixed(0)}%`}
                </span>
              </div>
            </div>
            <div className="flex flex-wrap gap-1">
              {group.trials.map((trial) => (
                <TrialIcon key={trial.id} trial={trial} />
              ))}
            </div>
          </div>
        );
      })}
      {task.latest_trials_truncated ? (
        <div className="text-muted-foreground text-[10px]">
          Showing the {task.latest_trials.length} most recent of{" "}
          {task.total_trials} trials.
        </div>
      ) : null}
    </div>
  );
}

export function TaskCard({ task }: { task: TaskBrowseItem }) {
  const { isSelected, toggle } = useSelection();
  const { mutate } = useSWRConfig();
  const selected = isSelected(task.id);

  function preserveBrowseSnapshot() {
    const snapshot = taskDetailFromBrowse(task);
    void mutate(
      taskDetailKey(task.id),
      (current: TaskDetailResource | undefined) =>
        current && !isBrowseTaskDetail(current) ? current : snapshot,
      { revalidate: false }
    );
  }

  return (
    <Card
      className={cn(
        "bg-card/95 border-[#6f88b4]/20 shadow-xs transition-colors hover:border-[#6f88b4]/40",
        selected && "border-[#6f88b4]/70 ring-1 ring-[#6f88b4]/40"
      )}
    >
      <CardHeader className="space-y-2 px-5 pt-5 pb-2">
        <div className="flex items-start justify-between gap-3">
          <Checkbox
            checked={selected}
            onCheckedChange={() => toggle(task)}
            aria-label={`Select ${task.name} for cost total`}
            className="mt-0.5 shrink-0"
          />
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <Link
                href={`/tasks/${encodeURIComponent(task.id)}`}
                onClick={preserveBrowseSnapshot}
                className="text-foreground font-mono text-sm font-semibold transition-colors hover:text-[#5d77a5] dark:hover:text-[#a8b8d2]"
              >
                {task.name}
              </Link>
              <Badge variant="outline" className="w-fit font-mono text-[11px]">
                v{task.current_version ?? "—"}
              </Badge>
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
                    onClick={(e) => e.stopPropagation()}
                    className={cn(
                      badgeVariants({ variant: "outline" }),
                      "hover:bg-accent w-fit gap-1.5 font-mono text-[11px] transition-colors"
                    )}
                  >
                    <GitPullRequest className="h-3 w-3 shrink-0" aria-hidden />
                    <span className="max-w-[140px] min-w-0 truncate">
                      {label}
                      {number && (
                        <span className="text-muted-foreground">
                          {" "}
                          #{number}
                        </span>
                      )}
                    </span>
                    <ExternalLink
                      className="h-3 w-3 shrink-0 opacity-50"
                      aria-hidden
                    />
                  </a>
                );
              })()}
            </div>
          </div>
          <div className="flex shrink-0 flex-col items-end gap-2">
            <Link
              href={`/tasks/${task.id}/probe`}
              target="_blank"
              rel="noopener noreferrer"
              className="text-muted-foreground hover:text-foreground text-[11px] font-medium underline-offset-2 hover:underline"
            >
              Probe run →
            </Link>
            <div className="text-right">
              <div className="text-muted-foreground text-[11px] tracking-wide uppercase">
                Last run
              </div>
              <div className="mt-1 text-xs">
                {task.last_run_at ? formatRelativeTime(task.last_run_at) : "—"}
              </div>
              {task.last_run_at ? (
                <div className="text-muted-foreground text-[11px]">
                  {formatShortDateTime(task.last_run_at)}
                </div>
              ) : null}
            </div>
            <div className="text-right">
              <div className="text-muted-foreground text-[11px] tracking-wide uppercase">
                Cost
              </div>
              <div className="mt-1 flex items-baseline gap-1.5 text-sm font-semibold tabular-nums">
                <span>
                  {task.cost_trial_count > 0 &&
                  hasDisplayableCostUsd(task.cost_usd)
                    ? `${task.cost_has_estimated && !task.cost_has_native ? "~" : ""}${formatCostUsd(task.cost_usd)}`
                    : "—"}
                </span>
                <QaCostSuffix
                  costUsd={task.qa_cost_usd}
                  title="QA/analysis spend for this task's trials. Not included in the cost figure."
                />
              </div>
              {task.cost_trial_count > 0 ? (
                <div className="text-muted-foreground text-[11px]">
                  {task.cost_trial_count} of {task.total_trials} priced
                </div>
              ) : null}
              {task.cost_trial_count > 0 ? (
                <div className="text-muted-foreground text-[11px]">
                  spent{" "}
                  {task.billed_trial_count === 0
                    ? formatCostUsd(0)
                    : `${task.billed_has_estimated && !task.billed_has_native ? "~" : ""}${formatCostUsd(task.billed_cost_usd)}`}
                </div>
              ) : null}
            </div>
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-3 px-5 pb-5">
        {(task.user_tags ?? []).length > 0 ? (
          <div className="flex flex-wrap gap-1">
            {(task.user_tags ?? []).map((t) => (
              <TagChip key={t.tag_id} tag={t} />
            ))}
          </div>
        ) : null}
        <div className="space-y-1.5">
          <div className="text-muted-foreground text-[11px] tracking-wide uppercase">
            Latest trials
          </div>
          <TrialGraphics task={task} />
        </div>
        <div className="grid gap-2.5 sm:grid-cols-[minmax(0,1fr)_minmax(0,1.45fr)]">
          <div className="border-border/60 bg-muted/30 rounded-md border px-3 py-2">
            <div className="text-muted-foreground text-[11px] tracking-wide uppercase">
              Avg score
            </div>
            <div className="mt-1 text-sm font-semibold">
              <PassRateCell task={task} />
            </div>
          </div>
          <div className="border-border/60 bg-muted/30 rounded-md border px-3 py-2">
            <div className="text-muted-foreground text-[11px] tracking-wide uppercase">
              Experiments
            </div>
            <div className="mt-0.5">
              <ExperimentsCell task={task} />
            </div>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
