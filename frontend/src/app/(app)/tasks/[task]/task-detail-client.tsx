"use client";

import { Fragment, useMemo, useState } from "react";
import Link from "next/link";
import useSWR from "swr";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { TaskFilesPanel } from "@/components/task-files-panel";
import { TrialDetailPanel } from "@/components/trial-detail-panel";
import { fetcher } from "@/lib/api";
import type { Task, TaskVersion, Trial } from "@/lib/types";
import { ChevronLeft, ChevronRight, X } from "lucide-react";

function formatNumber(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return value.toFixed(2);
}

function relativeTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const ms = Date.now() - new Date(iso).getTime();
  const seconds = Math.floor(ms / 1000);
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

function statusVariant(
  status: string
): "default" | "secondary" | "outline" | "destructive" {
  if (status === "success") return "secondary";
  if (status === "failed") return "destructive";
  if (status === "running") return "default";
  return "outline";
}

type AgentStat = {
  key: string;
  agent: string;
  model: string | null;
  provider: string;
  total: number;
  succeeded: number;
  failed: number;
  running: number;
  rewardSum: number;
  rewardCount: number;
  meanReward: number | null;
  passRate: number | null;
  lastRunAt: string | null;
};

const RUNNING_STATUSES = new Set(["pending", "queued", "running", "retrying"]);

export function TaskDetailClient({ taskId }: { taskId: string }) {
  const encoded = encodeURIComponent(taskId);
  const { data: task, error: taskError } = useSWR<Task>(
    `/api/tasks/${encoded}`,
    fetcher,
    { refreshInterval: 60000, revalidateOnFocus: false }
  );
  const { data: versions, error: versionsError } = useSWR<TaskVersion[]>(
    `/api/tasks/${encoded}/versions`,
    fetcher,
    { revalidateOnFocus: false }
  );

  const sortedVersions = useMemo(() => {
    if (!versions) return [];
    return [...versions].sort((a, b) => b.version - a.version);
  }, [versions]);

  const [selectedVersionId, setSelectedVersionId] = useState<string | null>(
    null
  );
  const activeVersionId = selectedVersionId ?? sortedVersions[0]?.id ?? null;
  const activeVersion = sortedVersions.find(
    (version) => version.id === activeVersionId
  );
  const trials = useMemo(() => task?.trials ?? [], [task?.trials]);

  const trialsForVersion = useMemo<Trial[]>(() => {
    if (!activeVersionId) return [];
    return [...trials]
      .filter((trial) => trial.task_version_id === activeVersionId)
      .sort((a, b) => {
        const av = a.finished_at ?? a.created_at ?? "";
        const bv = b.finished_at ?? b.created_at ?? "";
        return bv.localeCompare(av);
      });
  }, [trials, activeVersionId]);

  const versionStats = useMemo(() => {
    const total = trialsForVersion.length;
    let succeeded = 0;
    let failed = 0;
    let running = 0;
    let rewardSum = 0;
    let rewardCount = 0;
    for (const trial of trialsForVersion) {
      if (trial.status === "success") succeeded += 1;
      else if (trial.status === "failed") failed += 1;
      else if (RUNNING_STATUSES.has(trial.status)) running += 1;
      if (typeof trial.reward === "number") {
        rewardSum += trial.reward;
        rewardCount += 1;
      }
    }
    return {
      total,
      succeeded,
      failed,
      running,
      meanReward: rewardCount > 0 ? rewardSum / rewardCount : null,
      passRate: total > 0 ? succeeded / total : null,
    };
  }, [trialsForVersion]);

  const perAgentStats = useMemo<AgentStat[]>(() => {
    const byKey = new Map<string, AgentStat>();
    for (const trial of trialsForVersion) {
      const key = `${(trial.agent ?? "").toLowerCase()}|${(trial.model ?? "").toLowerCase()}|${(trial.provider ?? "").toLowerCase()}`;
      let row = byKey.get(key);
      if (!row) {
        row = {
          key,
          agent: trial.agent ?? "",
          model: trial.model ?? null,
          provider: trial.provider ?? "",
          total: 0,
          succeeded: 0,
          failed: 0,
          running: 0,
          rewardSum: 0,
          rewardCount: 0,
          meanReward: null,
          passRate: null,
          lastRunAt: null,
        };
        byKey.set(key, row);
      }
      row.total += 1;
      if (trial.status === "success") row.succeeded += 1;
      else if (trial.status === "failed") row.failed += 1;
      else if (RUNNING_STATUSES.has(trial.status)) row.running += 1;
      if (typeof trial.reward === "number") {
        row.rewardSum += trial.reward;
        row.rewardCount += 1;
      }
      const finishedAt = trial.finished_at ?? trial.created_at;
      if (finishedAt && (!row.lastRunAt || finishedAt > row.lastRunAt)) {
        row.lastRunAt = finishedAt;
      }
    }
    const rows = Array.from(byKey.values());
    for (const row of rows) {
      row.meanReward =
        row.rewardCount > 0 ? row.rewardSum / row.rewardCount : null;
      row.passRate = row.total > 0 ? row.succeeded / row.total : null;
    }
    rows.sort(
      (a, b) =>
        b.total - a.total ||
        `${a.agent}|${a.model ?? ""}`.localeCompare(
          `${b.agent}|${b.model ?? ""}`
        )
    );
    return rows;
  }, [trialsForVersion]);

  const [inspectingTrialId, setInspectingTrialId] = useState<string | null>(
    null
  );
  const inspectingTrial = useMemo<Trial | null>(
    () =>
      inspectingTrialId
        ? (trialsForVersion.find((trial) => trial.id === inspectingTrialId) ??
          null)
        : null,
    [trialsForVersion, inspectingTrialId]
  );
  const inspectingAgentSiblings = useMemo<Trial[]>(() => {
    if (!inspectingTrial) return [];
    const key = `${(inspectingTrial.agent ?? "").toLowerCase()}|${(inspectingTrial.model ?? "").toLowerCase()}|${(inspectingTrial.provider ?? "").toLowerCase()}`;
    return trialsForVersion.filter(
      (trial) =>
        `${(trial.agent ?? "").toLowerCase()}|${(trial.model ?? "").toLowerCase()}|${(trial.provider ?? "").toLowerCase()}` ===
        key
    );
  }, [inspectingTrial, trialsForVersion]);
  const inspectingIndex = useMemo(
    () =>
      inspectingTrialId
        ? inspectingAgentSiblings.findIndex(
            (trial) => trial.id === inspectingTrialId
          )
        : -1,
    [inspectingAgentSiblings, inspectingTrialId]
  );
  const prevTrial =
    inspectingIndex > 0 ? inspectingAgentSiblings[inspectingIndex - 1] : null;
  const nextTrial =
    inspectingIndex >= 0 && inspectingIndex < inspectingAgentSiblings.length - 1
      ? inspectingAgentSiblings[inspectingIndex + 1]
      : null;

  if (taskError) {
    return (
      <div className="p-4">
        <div className="border-destructive/40 bg-destructive/5 rounded-md border p-3 text-sm">
          Failed to load task: {String((taskError as Error).message)}
        </div>
      </div>
    );
  }

  return (
    <div className="mx-auto w-full max-w-(--breakpoint-2xl) p-4">
      <div className="bg-card overflow-hidden rounded-lg border">
        <div className="flex flex-wrap items-start justify-between gap-3 border-b px-5 py-4">
          <div className="min-w-0">
            <h1 className="font-mono text-[24px] font-semibold tracking-[-0.02em]">
              {task?.name ?? taskId}
            </h1>
            <div className="text-muted-foreground mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
              <span className="font-mono">{task?.id ?? taskId}</span>
              <span>·</span>
              <span>
                {sortedVersions.length} version
                {sortedVersions.length === 1 ? "" : "s"}
              </span>
              {activeVersion ? (
                <>
                  <span>·</span>
                  <span>created {relativeTime(activeVersion.created_at)}</span>
                  {activeVersion.content_hash ? (
                    <>
                      <span>·</span>
                      <span className="font-mono">
                        {activeVersion.content_hash.slice(0, 12)}
                      </span>
                    </>
                  ) : null}
                </>
              ) : null}
            </div>
            {task?.tags && Object.keys(task.tags).length > 0 ? (
              <div className="mt-2 flex flex-wrap gap-1">
                {Object.entries(task.tags)
                  .filter(([key]) => !key.startsWith("github_"))
                  .map(([key, value]) => (
                    <Link
                      key={key}
                      href={`/tasks?query=${encodeURIComponent(value)}`}
                      title={`Filter by ${key}=${value}`}
                    >
                      <Badge
                        variant="secondary"
                        className="hover:bg-muted cursor-pointer font-mono text-[10px]"
                      >
                        {key}={value}
                      </Badge>
                    </Link>
                  ))}
              </div>
            ) : null}
            {activeVersion?.message ? (
              <div className="text-muted-foreground mt-2 text-xs">
                {activeVersion.message}
              </div>
            ) : null}
          </div>
          <div className="flex shrink-0 items-center gap-2">
            {sortedVersions.length > 0 ? (
              <Select
                value={activeVersionId ?? undefined}
                onValueChange={(value) => {
                  setSelectedVersionId(value);
                  setInspectingTrialId(null);
                }}
              >
                <SelectTrigger className="h-9 w-[220px]">
                  <SelectValue placeholder="Select version" />
                </SelectTrigger>
                <SelectContent>
                  {sortedVersions.map((version) => (
                    <SelectItem key={version.id} value={version.id}>
                      v{version.version}
                      {version.message
                        ? ` - ${version.message.slice(0, 40)}`
                        : ""}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            ) : null}
          </div>
        </div>

        {versionsError ? (
          <div className="text-destructive border-b p-3 text-sm">
            Failed to load versions: {String((versionsError as Error).message)}
          </div>
        ) : null}

        <div className="grid min-h-[70vh] grid-cols-1 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.4fr)]">
          <div className="flex min-h-0 flex-col border-b lg:border-r lg:border-b-0">
            <div className="text-muted-foreground shrink-0 border-b px-5 py-2 text-[11px] font-semibold tracking-wide uppercase">
              Task files
            </div>
            <div className="min-h-0 flex-1 overflow-hidden">
              <TaskFilesPanel
                key={`task-files-${activeVersionId ?? "none"}`}
                isOpen
                onClose={() => {}}
                taskId={null}
                filesUrl={`/api/tasks/${encoded}/files`}
                filesVersion={activeVersion?.version ?? null}
                contentOnly
              />
            </div>
          </div>

          <div className="flex min-h-0 flex-col">
            {inspectingTrial && task ? (
              <>
                <div className="flex items-center justify-between gap-2 border-b px-5 py-2">
                  <div className="flex items-center gap-1">
                    <Button
                      size="icon"
                      variant="ghost"
                      className="h-7 w-7"
                      disabled={!prevTrial}
                      onClick={() =>
                        prevTrial && setInspectingTrialId(prevTrial.id)
                      }
                      title="Previous trial for this agent"
                    >
                      <ChevronLeft className="h-4 w-4" />
                    </Button>
                    <Button
                      size="icon"
                      variant="ghost"
                      className="h-7 w-7"
                      disabled={!nextTrial}
                      onClick={() =>
                        nextTrial && setInspectingTrialId(nextTrial.id)
                      }
                      title="Next trial for this agent"
                    >
                      <ChevronRight className="h-4 w-4" />
                    </Button>
                    <span className="text-muted-foreground ml-1 text-[11px]">
                      {inspectingIndex + 1} / {inspectingAgentSiblings.length}{" "}
                      for this agent
                    </span>
                  </div>
                  <Button
                    size="sm"
                    variant="ghost"
                    className="h-7"
                    onClick={() => setInspectingTrialId(null)}
                  >
                    <X className="mr-1 h-3.5 w-3.5" /> Close
                  </Button>
                </div>
                <div className="flex-1 overflow-hidden">
                  <TrialDetailPanel
                    isOpen
                    onClose={() => setInspectingTrialId(null)}
                    trial={inspectingTrial}
                    task={task}
                    orderedTrials={inspectingAgentSiblings}
                    trialIndex={inspectingIndex}
                    onNavigate={(trial) => setInspectingTrialId(trial.id)}
                    allowRetry
                    contentOnly
                  />
                </div>
              </>
            ) : (
              <>
                {versionStats.total > 0 ? (
                  <div className="bg-muted/30 grid grid-cols-2 gap-3 border-b px-5 py-3 text-sm sm:grid-cols-5">
                    <div>
                      <div className="text-muted-foreground text-[10px] uppercase">
                        total
                      </div>
                      <div className="font-mono">{versionStats.total}</div>
                    </div>
                    <div>
                      <div className="text-muted-foreground text-[10px] uppercase">
                        succeeded
                      </div>
                      <div className="font-mono text-emerald-700 dark:text-emerald-400">
                        {versionStats.succeeded}
                      </div>
                    </div>
                    <div>
                      <div className="text-muted-foreground text-[10px] uppercase">
                        failed
                      </div>
                      <div className="font-mono text-rose-600 dark:text-rose-400">
                        {versionStats.failed}
                      </div>
                    </div>
                    <div>
                      <div className="text-muted-foreground text-[10px] uppercase">
                        running
                      </div>
                      <div className="font-mono text-amber-600 dark:text-amber-400">
                        {versionStats.running}
                      </div>
                    </div>
                    <div>
                      <div className="text-muted-foreground text-[10px] uppercase">
                        avg reward
                      </div>
                      <div className="font-mono">
                        {formatNumber(versionStats.meanReward)}
                      </div>
                    </div>
                  </div>
                ) : null}

                {!task || !versions ? (
                  <Skeleton className="m-3 h-32" />
                ) : trialsForVersion.length === 0 ? (
                  <div className="text-muted-foreground p-6 text-center text-sm">
                    No trials for this version yet.
                  </div>
                ) : (
                  <div className="min-h-0 flex-1 overflow-y-auto">
                    <Table>
                      <TableHeader>
                        <TableRow>
                          <TableHead>Agent / trial</TableHead>
                          <TableHead>Status</TableHead>
                          <TableHead className="text-right">Reward</TableHead>
                          <TableHead className="text-right">Finished</TableHead>
                          <TableHead className="text-right">Inspect</TableHead>
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {perAgentStats.map((agent) => {
                          const agentTrials = trialsForVersion.filter(
                            (trial) =>
                              `${(trial.agent ?? "").toLowerCase()}|${(trial.model ?? "").toLowerCase()}|${(trial.provider ?? "").toLowerCase()}` ===
                              agent.key
                          );
                          return (
                            <Fragment key={agent.key}>
                              <TableRow className="bg-muted/40 hover:bg-muted/40">
                                <TableCell className="font-mono text-xs font-semibold">
                                  {agent.agent}
                                  {agent.model && agent.model !== "default"
                                    ? ` · ${agent.model}`
                                    : ""}
                                  {agent.provider &&
                                  agent.provider !== "default" ? (
                                    <span className="text-muted-foreground ml-1 text-[10px] font-normal">
                                      {agent.provider}
                                    </span>
                                  ) : null}
                                </TableCell>
                                <TableCell className="font-mono text-xs">
                                  <span className="text-emerald-700 dark:text-emerald-400">
                                    {agent.succeeded}
                                  </span>
                                  /{agent.total}
                                  {agent.failed > 0 ? (
                                    <span className="ml-1 text-rose-600 dark:text-rose-400">
                                      ({agent.failed} fail)
                                    </span>
                                  ) : null}
                                  {agent.running > 0 ? (
                                    <span className="ml-1 text-amber-600 dark:text-amber-400">
                                      ({agent.running} run)
                                    </span>
                                  ) : null}
                                </TableCell>
                                <TableCell className="text-muted-foreground text-right text-xs">
                                  {agent.passRate === null
                                    ? "—"
                                    : `${Math.round(agent.passRate * 100)}% pass`}
                                </TableCell>
                                <TableCell className="text-muted-foreground text-right text-xs">
                                  {relativeTime(agent.lastRunAt)}
                                </TableCell>
                                <TableCell />
                              </TableRow>
                              {agentTrials.map((trial) => (
                                <TableRow key={trial.id}>
                                  <TableCell className="text-muted-foreground pl-8 font-mono text-[11px]">
                                    {trial.id}
                                  </TableCell>
                                  <TableCell>
                                    <Badge
                                      variant={statusVariant(trial.status)}
                                    >
                                      {trial.status}
                                    </Badge>
                                  </TableCell>
                                  <TableCell className="text-right font-mono text-xs">
                                    {formatNumber(trial.reward)}
                                  </TableCell>
                                  <TableCell className="text-muted-foreground text-right text-xs">
                                    {relativeTime(trial.finished_at)}
                                  </TableCell>
                                  <TableCell className="text-right">
                                    <Button
                                      size="sm"
                                      variant="outline"
                                      className="h-7"
                                      onClick={() =>
                                        setInspectingTrialId(trial.id)
                                      }
                                    >
                                      Inspect
                                    </Button>
                                  </TableCell>
                                </TableRow>
                              ))}
                            </Fragment>
                          );
                        })}
                      </TableBody>
                    </Table>
                  </div>
                )}
              </>
            )}
          </div>
        </div>
      </div>

      <div className="mt-4 flex justify-end">
        <Button variant="ghost" size="sm" asChild>
          <Link href="/tasks">← All tasks</Link>
        </Button>
      </div>
    </div>
  );
}
