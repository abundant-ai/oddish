"use client";

import Link from "next/link";
import { ExternalLink, GitPullRequest } from "lucide-react";
import { Badge, badgeVariants } from "@/components/ui/badge";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { ExperimentsList } from "@/components/experiments-list";
import { TagChip } from "@/components/tag-chip";
import { formatCostUsd } from "@/lib/format";
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
  return task.latest_trials.reduce(
    (counts, trial) => {
      const status = getMatrixStatus(
        trial.status,
        trial.reward,
        trial.error_message,
      );
      counts[status] += 1;
      return counts;
    },
    // `satisfies` (not `as`) so a missing MatrixStatus key is a compile error:
    // omitting one made `counts[status]++` do `undefined + 1 = NaN` and dropped
    // those trials (e.g. skipped/scoreless) from the breakdown silently.
    {
      pass: 0,
      partial: 0,
      fail: 0,
      "harness-error": 0,
      scoreless: 0,
      skipped: 0,
      pending: 0,
      queued: 0,
      running: 0,
    } satisfies Record<ReturnType<typeof getMatrixStatus>, number>,
  );
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
      count: statusCounts.pending + statusCounts.queued + statusCounts.running,
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
                <span className="text-foreground font-mono">{item.count}</span>
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

function TrialGraphics({ task }: { task: TaskBrowseItem }) {
  if (task.latest_trials.length === 0) {
    return (
      <div className="border-border/70 text-muted-foreground rounded-md border border-dashed px-3 py-3 text-center text-xs">
        No latest-version trials yet.
      </div>
    );
  }

  return (
    <div className="flex flex-wrap gap-1">
      {task.latest_trials.map((trial) => {
        const status = getMatrixStatus(
          trial.status,
          trial.reward,
          trial.error_message,
        );
        const config = STATUS_CONFIG[status];
        const badgeLabel =
          status === "partial"
            ? formatPartialRewardBadgeValue(trial.reward)
            : null;

        return (
          <Tooltip key={trial.id}>
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
      })}
    </div>
  );
}

export function TaskCard({ task }: { task: TaskBrowseItem }) {
  const { isSelected, toggle } = useSelection();
  const selected = isSelected(task.id);

  return (
    <Card
      className={cn(
        "bg-card/95 border-[#6f88b4]/20 shadow-xs transition-colors hover:border-[#6f88b4]/40",
        selected && "border-[#6f88b4]/70 ring-1 ring-[#6f88b4]/40",
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
                      "hover:bg-accent w-fit gap-1.5 font-mono text-[11px] transition-colors",
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
              <div className="mt-1 text-sm font-semibold tabular-nums">
                {task.cost_trial_count > 0
                  ? `${task.cost_has_estimated && !task.cost_has_native ? "~" : ""}${formatCostUsd(task.cost_usd)}`
                  : "—"}
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
