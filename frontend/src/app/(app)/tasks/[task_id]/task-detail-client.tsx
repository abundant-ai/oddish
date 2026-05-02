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

        <div className="grid grid-cols-1 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.4fr)]">
          {/* Left: files panel (task bundle, or trial outputs when inspecting) */}
          <div className="border-b lg:border-b-0 lg:border-r">
            <div className="border-b px-5 py-2 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
              {inspectingTrial ? "Trial files" : "Task files"}
            </div>
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

          {/* Right: trials list, swapped for inline trial detail when inspecting */}
          <div className="flex min-h-[60vh] flex-col">
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
                <div className="flex items-center justify-between border-b px-5 py-2">
                  <span className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                    Trials on this version
                  </span>
                  <span className="text-xs text-muted-foreground">
                    {trialsForVersion.length}{" "}
                    {trialsForVersion.length === 1 ? "trial" : "trials"}
                  </span>
                </div>
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
                  <div className="overflow-y-auto">
                    <Table>
                      <TableHeader>
                        <TableRow>
                          <TableHead>Agent</TableHead>
                          <TableHead>Status</TableHead>
                          <TableHead>Reward</TableHead>
                          <TableHead>Finished</TableHead>
                          <TableHead className="text-right">Inspect</TableHead>
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {trialsForVersion.map((t) => (
                          <TableRow key={t.id}>
                            <TableCell className="font-mono text-xs">
                              {t.agent}
                              {t.model ? ` · ${t.model}` : ""}
                            </TableCell>
                            <TableCell>
                              <Badge variant={statusVariant(t.status)}>
                                {t.status}
                              </Badge>
                            </TableCell>
                            <TableCell className="font-mono text-xs">
                              {fmt(t.reward)}
                            </TableCell>
                            <TableCell className="text-xs text-muted-foreground">
                              {rel(t.finished_at)}
                            </TableCell>
                            <TableCell className="text-right">
                              <Button
                                size="sm"
                                variant="outline"
                                className="h-7"
                                onClick={() => setInspectingTrialId(t.id)}
                              >
                                Inspect
                              </Button>
                            </TableCell>
                          </TableRow>
                        ))}
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
