"use client";

import { useMemo, useState } from "react";
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
import { fetcher } from "@/lib/api";
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

type AgentKey = string;

interface AgentRow {
  key: AgentKey;
  agent: string;
  model: string | null;
  provider: string;
  total: number;
  succeeded: number;
  failed: number;
  running: number;
  meanReward: number | null;
  lastRunAt: string | null;
}

const RUNNING = new Set(["pending", "queued", "running", "retrying"]);

function agentKey(t: Trial): string {
  return `${(t.agent ?? "").toLowerCase()}|${(t.model ?? "").toLowerCase()}|${(
    t.provider ?? ""
  ).toLowerCase()}`;
}

function summarize(trials: Trial[]): AgentRow[] {
  const byKey = new Map<string, AgentRow & { rewardSum: number; rewardCount: number }>();
  for (const t of trials) {
    const k = agentKey(t);
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
        meanReward: null,
        lastRunAt: null,
        rewardSum: 0,
        rewardCount: 0,
      };
      byKey.set(k, row);
    }
    row.total += 1;
    if (t.status === "success") row.succeeded += 1;
    else if (t.status === "failed") row.failed += 1;
    else if (RUNNING.has(t.status)) row.running += 1;
    if (typeof t.reward === "number") {
      row.rewardSum += t.reward;
      row.rewardCount += 1;
    }
    if (t.finished_at && (!row.lastRunAt || t.finished_at > row.lastRunAt)) {
      row.lastRunAt = t.finished_at;
    }
  }
  const rows = Array.from(byKey.values());
  for (const r of rows) {
    r.meanReward = r.rewardCount > 0 ? r.rewardSum / r.rewardCount : null;
  }
  rows.sort((a, b) =>
    `${a.agent}|${a.model ?? ""}`.localeCompare(`${b.agent}|${b.model ?? ""}`),
  );
  return rows;
}

function fmt(n: number | null): string {
  return n === null ? "—" : n.toFixed(2);
}

function rel(iso: string | null): string {
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

export function TaskDetailClient({ taskId }: { taskId: string }) {
  const encoded = encodeURIComponent(taskId);

  const { data: task, error: taskError } = useSWR<Task>(
    `/api/tasks/${encoded}`,
    fetcher,
    { refreshInterval: 30_000 },
  );
  const { data: versions, error: versionsError } = useSWR<TaskVersion[]>(
    `/api/tasks/${encoded}/versions`,
    fetcher,
  );
  const { data: trials, error: trialsError } = useSWR<Trial[]>(
    `/api/tasks/${encoded}/trials`,
    fetcher,
    { refreshInterval: 30_000 },
  );

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

  const trialsForVersion = useMemo(() => {
    if (!trials || !activeVersionId) return [];
    return trials.filter((t) => t.task_version_id === activeVersionId);
  }, [trials, activeVersionId]);

  const summary = useMemo(() => summarize(trialsForVersion), [trialsForVersion]);

  const totalRewards = useMemo(() => {
    if (trialsForVersion.length === 0) return null;
    const scored = trialsForVersion.filter((t) => typeof t.reward === "number");
    if (scored.length === 0) return null;
    return (
      scored.reduce((acc, t) => acc + (t.reward ?? 0), 0) / scored.length
    );
  }, [trialsForVersion]);

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
    <div className="mx-auto w-full max-w-(--breakpoint-2xl) space-y-4 p-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="font-mono text-[26px] font-semibold tracking-[-0.02em]">
            {task?.name ?? taskId}
          </h1>
          <p className="text-xs text-muted-foreground">
            {task?.id ?? ""} · {sortedVersions.length} version
            {sortedVersions.length === 1 ? "" : "s"}
          </p>
        </div>
        <div className="flex items-center gap-2">
          {sortedVersions.length > 0 ? (
            <Select
              value={activeVersionId ?? undefined}
              onValueChange={(v) => setSelectedVersionId(v)}
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
        <div className="rounded-md border border-destructive/40 bg-destructive/5 p-3 text-sm">
          Failed to load versions: {String((versionsError as Error).message)}
        </div>
      ) : null}

      {activeVersion ? (
        <div className="rounded-md border bg-card p-4 text-sm">
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <div>
              <div className="text-[10px] uppercase text-muted-foreground">
                version
              </div>
              <div className="font-mono">v{activeVersion.version}</div>
            </div>
            <div>
              <div className="text-[10px] uppercase text-muted-foreground">
                content hash
              </div>
              <div className="font-mono text-xs">
                {activeVersion.content_hash
                  ? activeVersion.content_hash.slice(0, 16)
                  : "—"}
              </div>
            </div>
            <div>
              <div className="text-[10px] uppercase text-muted-foreground">
                created
              </div>
              <div>{rel(activeVersion.created_at)}</div>
            </div>
            <div>
              <div className="text-[10px] uppercase text-muted-foreground">
                evidence (mean reward)
              </div>
              <div className="font-mono">{fmt(totalRewards)}</div>
            </div>
          </div>
          {activeVersion.message ? (
            <div className="mt-3 text-xs text-muted-foreground">
              {activeVersion.message}
            </div>
          ) : null}
        </div>
      ) : null}

      <div className="rounded-md border">
        <div className="border-b px-4 py-2 text-xs font-medium text-muted-foreground">
          Agents tested against this version
        </div>
        {trialsError ? (
          <div className="p-3 text-sm">
            Failed to load trials: {String((trialsError as Error).message)}
          </div>
        ) : !trials || !versions ? (
          <Skeleton className="m-3 h-32" />
        ) : summary.length === 0 ? (
          <div className="p-6 text-center text-sm text-muted-foreground">
            No trials for this version yet.
          </div>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Agent</TableHead>
                <TableHead>Provider</TableHead>
                <TableHead>Trials</TableHead>
                <TableHead>Mean reward</TableHead>
                <TableHead>Last run</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {summary.map((row) => (
                <TableRow key={row.key}>
                  <TableCell className="font-mono text-xs">
                    {row.agent}
                    {row.model ? ` · ${row.model}` : ""}
                  </TableCell>
                  <TableCell className="text-xs">{row.provider}</TableCell>
                  <TableCell className="font-mono text-xs">
                    <span className="text-emerald-700 dark:text-emerald-400">
                      {row.succeeded}
                    </span>
                    /{row.total}
                    {row.failed > 0 ? (
                      <span className="ml-1 text-rose-600 dark:text-rose-400">
                        ({row.failed} failed)
                      </span>
                    ) : null}
                    {row.running > 0 ? (
                      <span className="ml-1 text-amber-600 dark:text-amber-400">
                        ({row.running} running)
                      </span>
                    ) : null}
                  </TableCell>
                  <TableCell className="font-mono text-xs">
                    {fmt(row.meanReward)}
                  </TableCell>
                  <TableCell className="text-xs text-muted-foreground">
                    {rel(row.lastRunAt)}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </div>

      <div className="flex justify-end">
        <Button variant="ghost" size="sm" asChild>
          <Link href="/tasks">← All tasks</Link>
        </Button>
      </div>
    </div>
  );
}
