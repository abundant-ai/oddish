"use client";

import { Fragment, useMemo, useState } from "react";
import useSWR from "swr";
import Link from "next/link";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { TaskFilesPanel } from "@/components/task-files-panel";
import { TrialDetailPanel } from "@/components/trial-detail-panel";
import { fetcher } from "@/lib/api";
import { ChevronLeft, ChevronRight, X } from "lucide-react";
import type { Task, Trial } from "@/lib/types";

interface TaskVersion {
  id: string;
  task_id: string;
  version: number;
  task_path: string;
  task_s3_key: string | null;
  content_hash: string | null;
  message: string | null;
  created_by_user_id: string | null;
  created_at: string;
}

function fmt(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  return n.toFixed(2);
}

function rel(iso: string | null | undefined): string {
  if (!iso) return "—";
  const ms = Date.now() - new Date(iso).getTime();
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

function statusVariant(
  status: string,
): "default" | "secondary" | "outline" | "destructive" {
  if (status === "success") return "secondary";
  if (status === "failed") return "destructive";
  if (status === "running") return "default";
  return "outline";
}

export function TaskDetailClient({ taskId }: { taskId: string }) {
  const encoded = encodeURIComponent(taskId);

  // /api/tasks/{id} returns the task with ``trials`` embedded; one
  // fetch is enough. /trials is intentionally not fetched separately.
  const { data: task, error: taskError } = useSWR<Task>(
    `/api/tasks/${encoded}`,
    fetcher,
    { refreshInterval: 60_000, revalidateOnFocus: false },
  );
  const { data: versions, error: versionsError } = useSWR<TaskVersion[]>(
    `/api/tasks/${encoded}/versions`,
    fetcher,
    { revalidateOnFocus: false },
  );
  const trials: Trial[] | undefined = task?.trials ?? undefined;
  const trialsError = taskError;

  const sortedVersions = useMemo(() => {
    if (!versions) return [];
    return [...versions].sort((a, b) => b.version - a.version);
  }, [versions]);

  const [selectedVersionId, setSelectedVersionId] = useState<string | null>(
    null,
  );
  const activeVersionId =
    selectedVersionId ?? sortedVersions[0]?.id ?? null;
  const activeVersion = sortedVersions.find((v) => v.id === activeVersionId);

  const trialsForVersion = useMemo<Trial[]>(() => {
    if (!trials || !activeVersionId) return [];
    return [...trials]
      .filter((t) => t.task_version_id === activeVersionId)
      .sort((a, b) => {
        const av = a.finished_at ?? a.created_at ?? "";
        const bv = b.finished_at ?? b.created_at ?? "";
        return bv.localeCompare(av);
      });
  }, [trials, activeVersionId]);

  const RUNNING_STATUSES = useMemo(
    () => new Set(["pending", "queued", "running", "retrying"]),
    [],
  );

  const versionStats = useMemo(() => {
    const total = trialsForVersion.length;
    let succeeded = 0;
    let failed = 0;
    let running = 0;
    let rewardSum = 0;
    let rewardCount = 0;
    for (const t of trialsForVersion) {
      if (t.status === "success") succeeded += 1;
      else if (t.status === "failed") failed += 1;
      else if (RUNNING_STATUSES.has(t.status)) running += 1;
      if (typeof t.reward === "number") {
        rewardSum += t.reward;
        rewardCount += 1;
      }
    }
    const meanReward = rewardCount > 0 ? rewardSum / rewardCount : null;
    const passRate = total > 0 ? succeeded / total : null;
    return { total, succeeded, failed, running, meanReward, passRate };
  }, [trialsForVersion, RUNNING_STATUSES]);

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

  const perAgentStats = useMemo<AgentStat[]>(() => {
    const byKey = new Map<string, AgentStat>();
    for (const t of trialsForVersion) {
      const k = `${(t.agent ?? "").toLowerCase()}|${(t.model ?? "").toLowerCase()}|${(t.provider ?? "").toLowerCase()}`;
      let row = byKey.get(k);
      if (!row) {
        row = {
          key: k,
          agent: t.agent ?? "",
          model: t.model ?? null,
          provider: t.provider ?? "",
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
        byKey.set(k, row);
      }
      row.total += 1;
      if (t.status === "success") row.succeeded += 1;
      else if (t.status === "failed") row.failed += 1;
      else if (RUNNING_STATUSES.has(t.status)) row.running += 1;
      if (typeof t.reward === "number") {
        row.rewardSum += t.reward;
        row.rewardCount += 1;
      }
      const fa = t.finished_at ?? t.created_at;
      if (fa && (!row.lastRunAt || fa > row.lastRunAt)) row.lastRunAt = fa;
    }
    const rows = Array.from(byKey.values());
    for (const r of rows) {
      r.meanReward = r.rewardCount > 0 ? r.rewardSum / r.rewardCount : null;
      r.passRate = r.total > 0 ? r.succeeded / r.total : null;
    }
    rows.sort(
      (a, b) =>
        b.total - a.total ||
        `${a.agent}|${a.model ?? ""}`.localeCompare(
          `${b.agent}|${b.model ?? ""}`,
        ),
    );
    return rows;
  }, [trialsForVersion, RUNNING_STATUSES]);

  const [inspectingTrialId, setInspectingTrialId] = useState<string | null>(
    null,
  );

  const inspectingTrial = useMemo<Trial | null>(
    () =>
      inspectingTrialId
        ? trialsForVersion.find((t) => t.id === inspectingTrialId) ?? null
        : null,
    [trialsForVersion, inspectingTrialId],
  );
  const inspectingIndex = useMemo(
    () =>
      inspectingTrialId
        ? trialsForVersion.findIndex((t) => t.id === inspectingTrialId)
        : -1,
    [trialsForVersion, inspectingTrialId],
  );
  const prevTrial =
    inspectingIndex > 0 ? trialsForVersion[inspectingIndex - 1] : null;
  const nextTrial =
    inspectingIndex >= 0 && inspectingIndex < trialsForVersion.length - 1
      ? trialsForVersion[inspectingIndex + 1]
      : null;

  if (taskError) {
    return (
      <div className="p-4">
        <div className="rounded-md border border-destructive/40 bg-destructive/5 p-3 text-sm">
          Failed to load task: {String((taskError as Error).message)}
        </div>
      </div>
    );
  }

  return (
    <div className="mx-auto w-full max-w-(--breakpoint-2xl) p-4">
      <div className="overflow-hidden rounded-lg border bg-card">
        {/* Header strip: title, tags, version + small meta inline */}
        <div className="flex flex-wrap items-start justify-between gap-3 border-b px-5 py-4">
          <div className="min-w-0">
            <h1 className="font-mono text-[24px] font-semibold tracking-[-0.02em]">
              {task?.name ?? taskId}
            </h1>
            <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
              <span className="font-mono">{task?.id ?? ""}</span>
              <span>·</span>
              <span>
                {sortedVersions.length} version
                {sortedVersions.length === 1 ? "" : "s"}
              </span>
              {activeVersion ? (
                <>
                  <span>·</span>
                  <span>created {rel(activeVersion.created_at)}</span>
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
                  .filter(([k]) => !k.startsWith("github_"))
                  .map(([k, v]) => (
                    <Link
                      key={k}
                      href={`/tasks?query=${encodeURIComponent(v)}`}
                      title={`Filter by ${k}=${v}`}
                    >
                      <Badge
                        variant="secondary"
                        className="cursor-pointer font-mono text-[10px] hover:bg-muted"
                      >
                        {k}={v}
                      </Badge>
                    </Link>
                  ))}
              </div>
            ) : null}
            {activeVersion?.message ? (
              <div className="mt-2 text-xs text-muted-foreground">
                {activeVersion.message}
              </div>
            ) : null}
          </div>
          <div className="flex shrink-0 items-center gap-2">
            {sortedVersions.length > 0 ? (
              <Select
                value={activeVersionId ?? undefined}
                onValueChange={(v) => {
                  setSelectedVersionId(v);
                  setInspectingTrialId(null);
                }}
              >
                <SelectTrigger className="h-9 w-[220px]">
                  <SelectValue placeholder="Select version" />
                </SelectTrigger>
                <SelectContent>
                  {sortedVersions.map((v) => (
                    <SelectItem key={v.id} value={v.id}>
                      v{v.version}
                      {v.message ? ` — ${v.message.slice(0, 40)}` : ""}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            ) : null}
          </div>
        </div>

        {versionsError ? (
          <div className="border-b p-3 text-sm text-destructive">
            Failed to load versions: {String((versionsError as Error).message)}
          </div>
        ) : null}

        <div className="grid min-h-[70vh] grid-cols-1 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.4fr)]">
          {/* Left: files panel (task bundle, or trial outputs when inspecting) */}
          <div className="flex min-h-0 flex-col border-b lg:border-b-0 lg:border-r">
            <div className="shrink-0 border-b px-5 py-2 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
              {inspectingTrial ? "Trial files" : "Task files"}
            </div>
            <div className="min-h-0 flex-1 overflow-hidden">
              {inspectingTrial ? (
                <TaskFilesPanel
                  key={`trial-files-${inspectingTrial.id}`}
                  isOpen={true}
                  onClose={() => {}}
                  taskId={null}
                  filesUrl={`/api/trials/${encodeURIComponent(inspectingTrial.id)}/files`}
                  contentOnly
                />
              ) : (
                <TaskFilesPanel
                  key={`task-files-${activeVersionId ?? "none"}`}
                  isOpen={true}
                  onClose={() => {}}
                  taskId={taskId}
                  task={task ?? null}
                  contentOnly
                />
              )}
            </div>
          </div>

          {/* Right: trials list, swapped for inline trial detail when inspecting */}
          <div className="flex min-h-0 flex-col">
            {inspectingTrial && task ? (
              <>
                <div className="flex items-center gap-2 border-b px-5 py-2">
                  <Button
                    size="icon"
                    variant="ghost"
                    className="h-7 w-7"
                    disabled={!prevTrial}
                    onClick={() =>
                      prevTrial && setInspectingTrialId(prevTrial.id)
                    }
                    title="Previous trial"
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
                    title="Next trial"
                  >
                    <ChevronRight className="h-4 w-4" />
                  </Button>
                  <div className="flex-1 truncate text-xs text-muted-foreground">
                    Trial {inspectingIndex + 1} of {trialsForVersion.length} ·{" "}
                    <span className="font-mono">{inspectingTrial.id}</span>
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
                    isOpen={true}
                    onClose={() => setInspectingTrialId(null)}
                    trial={inspectingTrial}
                    task={task}
                    orderedTrials={trialsForVersion}
                    trialIndex={inspectingIndex}
                    onNavigate={(t) => setInspectingTrialId(t.id)}
                    allowRetry
                    contentOnly
                    hideFilesTab
                  />
                </div>
              </>
            ) : (
              <>
                {versionStats.total > 0 ? (
                  <div className="grid grid-cols-2 gap-3 border-b bg-muted/30 px-5 py-3 text-sm sm:grid-cols-5">
                    <div>
                      <div className="text-[10px] uppercase text-muted-foreground">
                        total
                      </div>
                      <div className="font-mono">{versionStats.total}</div>
                    </div>
                    <div>
                      <div className="text-[10px] uppercase text-muted-foreground">
                        succeeded
                      </div>
                      <div className="font-mono text-emerald-700 dark:text-emerald-400">
                        {versionStats.succeeded}
                      </div>
                    </div>
                    <div>
                      <div className="text-[10px] uppercase text-muted-foreground">
                        failed
                      </div>
                      <div className="font-mono text-rose-600 dark:text-rose-400">
                        {versionStats.failed}
                      </div>
                    </div>
                    <div>
                      <div className="text-[10px] uppercase text-muted-foreground">
                        running
                      </div>
                      <div className="font-mono text-amber-600 dark:text-amber-400">
                        {versionStats.running}
                      </div>
                    </div>
                    <div>
                      <div className="text-[10px] uppercase text-muted-foreground">
                        μ reward
                      </div>
                      <div className="font-mono">
                        {fmt(versionStats.meanReward)}
                      </div>
                    </div>
                  </div>
                ) : null}

                {trialsError ? (
                  <div className="p-3 text-sm">
                    Failed to load trials:{" "}
                    {String((trialsError as Error).message)}
                  </div>
                ) : !trials || !versions ? (
                  <Skeleton className="m-3 h-32" />
                ) : trialsForVersion.length === 0 ? (
                  <div className="p-6 text-center text-sm text-muted-foreground">
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
                            (t) =>
                              `${(t.agent ?? "").toLowerCase()}|${(t.model ?? "").toLowerCase()}|${(t.provider ?? "").toLowerCase()}` ===
                              agent.key,
                          );
                          return (
                            <Fragment key={agent.key}>
                              <TableRow
                                className="bg-muted/40 hover:bg-muted/40"
                              >
                                <TableCell className="font-mono text-xs font-semibold">
                                  {agent.agent}
                                  {agent.model ? ` · ${agent.model}` : ""}
                                  <span className="ml-1 text-[10px] font-normal text-muted-foreground">
                                    {agent.provider}
                                  </span>
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
                                <TableCell className="text-right font-mono text-xs">
                                  μ {fmt(agent.meanReward)}
                                </TableCell>
                                <TableCell className="text-right text-xs text-muted-foreground">
                                  {agent.passRate === null
                                    ? "—"
                                    : `${Math.round(agent.passRate * 100)}% pass`}
                                </TableCell>
                                <TableCell className="text-right text-xs text-muted-foreground">
                                  {rel(agent.lastRunAt)}
                                </TableCell>
                              </TableRow>
                              {agentTrials.map((t) => (
                                <TableRow key={t.id}>
                                  <TableCell className="pl-8 font-mono text-[11px] text-muted-foreground">
                                    {t.id}
                                  </TableCell>
                                  <TableCell>
                                    <Badge variant={statusVariant(t.status)}>
                                      {t.status}
                                    </Badge>
                                  </TableCell>
                                  <TableCell className="text-right font-mono text-xs">
                                    {fmt(t.reward)}
                                  </TableCell>
                                  <TableCell className="text-right text-xs text-muted-foreground">
                                    {rel(t.finished_at)}
                                  </TableCell>
                                  <TableCell className="text-right">
                                    <Button
                                      size="sm"
                                      variant="outline"
                                      className="h-7"
                                      onClick={() =>
                                        setInspectingTrialId(t.id)
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
