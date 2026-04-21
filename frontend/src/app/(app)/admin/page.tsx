"use client";

import { useMemo, useState } from "react";
import useSWR from "swr";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Alert, AlertTitle, AlertDescription } from "@/components/ui/alert";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import type {
  QueueSlotsResponse,
  QueueStatusResponse,
  OrphanedStateResponse,
  QueueSlotSummary,
} from "@/lib/types";
import { fetcher } from "@/lib/api";
import { QueueKeyIcon } from "@/components/queue-key-icon";
import { WorkerJobsCard } from "@/components/worker-jobs-card";
import { RefreshCw, Server, Clock, AlertCircle } from "lucide-react";

const formatAge = (dateStr: string | null) => {
  if (!dateStr) return "—";
  const diffMs = Date.now() - new Date(dateStr).getTime();
  if (diffMs <= 0) return "0s";
  const totalSeconds = Math.floor(diffMs / 1000);
  if (totalSeconds < 60) return `${totalSeconds}s`;
  if (totalSeconds < 3600) return `${Math.floor(totalSeconds / 60)}m`;
  if (totalSeconds < 86400) return `${Math.floor(totalSeconds / 3600)}h`;
  return `${Math.floor(totalSeconds / 86400)}d`;
};


// =============================================================================
// Queue Slots Card
// =============================================================================

function QueueSlotsCard() {
  const { data, error, isLoading, mutate } = useSWR<QueueSlotsResponse>(
    `/api/admin/slots`,
    fetcher,
    {
      refreshInterval: 10000,
    },
  );

  const formatTimestamp = (ts: string | null) => {
    if (!ts) return "—";
    const date = new Date(ts);
    const now = new Date();
    const diffMs = date.getTime() - now.getTime();
    const diffSec = Math.round(diffMs / 1000);

    if (diffSec < 0) return "Expired";
    if (diffSec < 60) return `${diffSec}s`;
    if (diffSec < 3600) return `${Math.round(diffSec / 60)}m`;
    return `${Math.round(diffSec / 3600)}h`;
  };
  const [queueFilter, setQueueFilter] = useState("");
  const filteredProviders = useMemo(() => {
    if (!data?.queue_keys) return [];
    const query = queueFilter.toLowerCase().trim();
    if (!query) return data.queue_keys;
    return data.queue_keys.filter((queueSummary) => {
      const queueKey = queueSummary.queue_key;
      return queueKey.toLowerCase().includes(query);
    });
  }, [data?.queue_keys, queueFilter]);

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Server className="h-5 w-5" />
            <CardTitle className="text-base">Queue Slots</CardTitle>
            {data && (
              <Badge variant="outline" className="text-xs">
                {data.total_active}/{data.total_slots} active
              </Badge>
            )}
          </div>
          <Button
            variant="outline"
            size="sm"
            onClick={() => mutate()}
            disabled={isLoading}
          >
            <RefreshCw
              className={`h-4 w-4 ${isLoading ? "animate-spin" : ""}`}
            />
          </Button>
        </div>
        {data && (
          <p className="text-xs text-muted-foreground">
            Last updated: {new Date(data.timestamp).toLocaleTimeString()}
          </p>
        )}
      </CardHeader>
      <CardContent>
        {error ? (
          <Alert variant="destructive">
            <AlertCircle className="h-4 w-4" />
            <AlertTitle>Failed to load queue slots</AlertTitle>
            <AlertDescription>
              {error instanceof Error
                ? error.message
                : "Check if you have admin access."}
            </AlertDescription>
          </Alert>
        ) : isLoading ? (
          <p className="text-muted-foreground">Loading...</p>
        ) : !data || data.queue_keys.length === 0 ? (
          <div className="py-8 text-center text-muted-foreground">
            <Server className="mx-auto mb-3 h-12 w-12 opacity-50" />
            <p>No queue slots configured</p>
          </div>
        ) : (
          <div className="space-y-3">
            <Input
              value={queueFilter}
              onChange={(event) => setQueueFilter(event.target.value)}
              placeholder="Filter queue keys..."
              className="h-8 text-xs"
            />
            <Accordion type="multiple" className="space-y-2">
              {filteredProviders.map((queueSummary: QueueSlotSummary) => {
                const queueKey = queueSummary.queue_key;
                return (
                  <AccordionItem
                    key={queueKey}
                    value={queueKey}
                    className="rounded-lg border px-3"
                  >
                    <AccordionTrigger className="py-3 hover:no-underline">
                      <div className="flex w-full items-center justify-between pr-2">
                        <span className="font-medium">
                          <span className="inline-flex items-center gap-2">
                            <QueueKeyIcon queueKey={queueKey} size={13} />
                            <span className="font-mono text-xs">
                              {queueKey}
                            </span>
                          </span>
                        </span>
                        <div className="flex items-center gap-2">
                          <div className="flex gap-1">
                            {Array.from({
                              length: queueSummary.total_slots,
                            }).map((_, i) => (
                              <div
                                key={i}
                                className={`h-2 w-2 rounded-full ${
                                  i < queueSummary.active_slots
                                    ? "bg-blue-500"
                                    : "bg-muted-foreground/30"
                                }`}
                              />
                            ))}
                          </div>
                          <Badge
                            variant={
                              queueSummary.active_slots > 0
                                ? "default"
                                : "outline"
                            }
                            className="text-xs"
                          >
                            {queueSummary.active_slots}/
                            {queueSummary.total_slots}
                          </Badge>
                        </div>
                      </div>
                    </AccordionTrigger>
                    <AccordionContent>
                      <Table>
                        <TableHeader>
                          <TableRow>
                            <TableHead className="w-16">Slot</TableHead>
                            <TableHead>Worker ID</TableHead>
                            <TableHead className="text-right">
                              Expires In
                            </TableHead>
                          </TableRow>
                        </TableHeader>
                        <TableBody>
                          {queueSummary.slots.map((slot) => (
                            <TableRow
                              key={slot.slot}
                              className={slot.is_active ? "" : "opacity-50"}
                            >
                              <TableCell className="font-mono text-xs">
                                #{slot.slot}
                              </TableCell>
                              <TableCell className="font-mono text-xs">
                                {slot.locked_by || "—"}
                              </TableCell>
                              <TableCell className="text-right text-xs">
                                {slot.is_active ? (
                                  <Badge variant="outline" className="text-xs">
                                    <Clock className="mr-1 h-3 w-3" />
                                    {formatTimestamp(slot.locked_until)}
                                  </Badge>
                                ) : (
                                  <span className="text-muted-foreground">
                                    Available
                                  </span>
                                )}
                              </TableCell>
                            </TableRow>
                          ))}
                        </TableBody>
                      </Table>
                    </AccordionContent>
                  </AccordionItem>
                );
              })}
            </Accordion>
            {filteredProviders.length === 0 && (
              <p className="text-xs text-muted-foreground">
                No queue keys match the current filter.
              </p>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

// =============================================================================
// Queue Health Summary Card
// =============================================================================

function QueueHealthCard() {
  const {
    data: slotsData,
    error: slotsError,
    isLoading: slotsLoading,
  } = useSWR<QueueSlotsResponse>("/api/admin/slots", fetcher, {
    refreshInterval: 10000,
  });

  const {
    data: qsData,
    error: qsError,
    isLoading: qsLoading,
  } = useSWR<QueueStatusResponse>("/api/admin/queue-status", fetcher, {
    refreshInterval: 10000,
  });
  const [queueFilter, setQueueFilter] = useState("");

  const queueKeys = new Set<string>();
  slotsData?.queue_keys.forEach((p) => queueKeys.add(p.queue_key));
  qsData?.trial_queues?.forEach((q) => queueKeys.add(q.queue_key));

  const queueRows = Array.from(queueKeys).map((queueKey) => {
    const slotSummary =
      slotsData?.queue_keys.find((p) => p.queue_key === queueKey) ?? null;
    const trialEntry = qsData?.trial_queues?.find(
      (q) => q.queue_key === queueKey,
    );
    const queued = trialEntry?.queued ?? 0;
    const running = trialEntry?.running ?? 0;
    const totalSlots = slotSummary?.total_slots ?? 0;
    const activeSlots = slotSummary?.active_slots ?? 0;
    const staleLocks =
      slotSummary?.slots.filter((slot) => slot.locked_by && !slot.is_active)
        .length ?? 0;

    const notes: string[] = [];
    if (totalSlots === 0 && (queued > 0 || running > 0)) {
      notes.push("No slots configured");
    }
    if (queued > 0 && totalSlots > 0 && activeSlots === 0) {
      notes.push("No active workers");
    }
    if (queued > 0 && totalSlots > 0 && activeSlots >= totalSlots) {
      notes.push("At capacity");
    }
    if (staleLocks > 0) {
      notes.push(`${staleLocks} stale lock${staleLocks > 1 ? "s" : ""}`);
    }

    return {
      queueKey,
      queued,
      running,
      totalSlots,
      activeSlots,
      notes,
    };
  });
  const filteredRows = queueRows
    .filter((row) =>
      row.queueKey.toLowerCase().includes(queueFilter.toLowerCase().trim()),
    )
    .sort((a, b) => b.queued + b.running - (a.queued + a.running))
    .slice(0, 30);

  const totalQueued = queueRows.reduce((sum, row) => sum + row.queued, 0);
  const totalRunning = queueRows.reduce((sum, row) => sum + row.running, 0);
  const totalSlots = slotsData?.total_slots ?? 0;
  const totalActive = slotsData?.total_active ?? 0;

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Server className="h-5 w-5" />
            <CardTitle className="text-base">Queue Health</CardTitle>
            {slotsData && (
              <Badge variant="outline" className="text-xs">
                {totalActive}/{totalSlots} slots active
              </Badge>
            )}
          </div>
          {qsData && (
            <div className="flex gap-2">
              <Badge variant="outline" className="text-xs">
                {totalQueued} queued
              </Badge>
              <Badge variant="outline" className="text-xs">
                {totalRunning} running
              </Badge>
            </div>
          )}
        </div>
        {qsData && (
          <p className="text-xs text-muted-foreground">
            Last updated: {new Date(qsData.timestamp).toLocaleTimeString()}
          </p>
        )}
      </CardHeader>
      <CardContent>
        {slotsError || qsError ? (
          <Alert variant="destructive">
            <AlertCircle className="h-4 w-4" />
            <AlertTitle>Failed to load queue health</AlertTitle>
            <AlertDescription>
              {slotsError instanceof Error
                ? slotsError.message
                : qsError instanceof Error
                  ? qsError.message
                  : "Check if you have admin access."}
            </AlertDescription>
          </Alert>
        ) : slotsLoading || qsLoading ? (
          <p className="text-muted-foreground">Loading...</p>
        ) : queueRows.length === 0 ? (
          <div className="py-6 text-center text-muted-foreground">
            <Server className="mx-auto mb-2 h-10 w-10 opacity-50" />
            <p>No queue data available</p>
          </div>
        ) : (
          <div className="space-y-3">
            <Input
              value={queueFilter}
              onChange={(event) => setQueueFilter(event.target.value)}
              placeholder="Filter queue keys..."
              className="h-8 text-xs"
            />
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Queue Key</TableHead>
                  <TableHead className="text-right">Queued</TableHead>
                  <TableHead className="text-right">Running</TableHead>
                  <TableHead className="text-right">Slots</TableHead>
                  <TableHead>Notes</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {filteredRows.map((row) => (
                  <TableRow key={row.queueKey}>
                    <TableCell>
                      <span className="inline-flex items-center gap-2">
                        <QueueKeyIcon queueKey={row.queueKey} size={13} />
                        <span className="font-mono text-xs">
                          {row.queueKey}
                        </span>
                      </span>
                    </TableCell>
                    <TableCell className="text-right font-mono text-xs">
                      {row.queued}
                    </TableCell>
                    <TableCell className="text-right font-mono text-xs">
                      {row.running}
                    </TableCell>
                    <TableCell className="text-right text-xs">
                      {row.activeSlots}/{row.totalSlots || "—"}
                    </TableCell>
                    <TableCell className="text-xs text-muted-foreground">
                      {row.notes.length > 0 ? row.notes.join(" • ") : "—"}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
            <p className="text-xs text-muted-foreground">
              Running tracks active workers. If queued &gt; 0 with no active
              slots, workers are not spawning or slots are locked.
            </p>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function formatIssueLabel(issue: string) {
  switch (issue) {
    case "running_stale_heartbeat":
      return "Running trial with stale heartbeat";
    case "active_task_without_active_trials":
      return "Active task without active trials";
    default:
      return issue.replaceAll("_", " ");
  }
}

function OrphanedStateCard() {
  const { data, error, isLoading } = useSWR<OrphanedStateResponse>(
    "/api/admin/orphaned-state?stale_after_minutes=10",
    fetcher,
    {
      refreshInterval: 10000,
    },
  );

  const counts = data?.counts;
  const totalIssues = counts
    ? counts.running_stale_heartbeat + counts.active_tasks_without_active_trials
    : 0;

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <AlertCircle className="h-5 w-5" />
            <CardTitle className="text-base">Orphaned State</CardTitle>
            {counts && (
              <Badge variant={totalIssues > 0 ? "destructive" : "outline"}>
                {totalIssues} signals
              </Badge>
            )}
          </div>
          {data && (
            <p className="text-xs text-muted-foreground">
              Updated {new Date(data.timestamp).toLocaleTimeString()}
            </p>
          )}
        </div>
        {data && (
          <p className="text-xs text-muted-foreground">
            Heartbeat becomes stale after {data.stale_after_minutes} minutes.
          </p>
        )}
      </CardHeader>
      <CardContent>
        {error ? (
          <Alert variant="destructive">
            <AlertCircle className="h-4 w-4" />
            <AlertTitle>Failed to load orphaned state</AlertTitle>
            <AlertDescription>
              {error instanceof Error ? error.message : "Unknown error"}
            </AlertDescription>
          </Alert>
        ) : isLoading || !counts ? (
          <p className="text-muted-foreground">Loading...</p>
        ) : (
          <div className="space-y-4">
            <div className="flex flex-wrap gap-2 text-xs">
              <Badge variant="outline">
                stale-heartbeat {counts.running_stale_heartbeat}
              </Badge>
              <Badge variant="outline">
                stuck-tasks {counts.active_tasks_without_active_trials}
              </Badge>
            </div>

            {totalIssues === 0 ? (
              <p className="text-sm text-muted-foreground">
                No orphaned queue or pipeline state detected.
              </p>
            ) : (
              <>
                <div className="space-y-2">
                  <div className="text-sm font-medium">
                    Execution job samples
                  </div>
                  {data.trial_samples.length === 0 ? (
                    <p className="text-xs text-muted-foreground">
                      No execution job samples.
                    </p>
                  ) : (
                    <Table>
                      <TableHeader>
                        <TableRow>
                          <TableHead>Issue</TableHead>
                          <TableHead>Execution Job</TableHead>
                          <TableHead>Queue Key</TableHead>
                          <TableHead>Worker</TableHead>
                          <TableHead>Heartbeat</TableHead>
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {data.trial_samples.map((sample) => (
                          <TableRow key={`${sample.issue}-${sample.trial_id}`}>
                            <TableCell className="text-xs">
                              {formatIssueLabel(sample.issue)}
                            </TableCell>
                            <TableCell className="font-mono text-xs">
                              {sample.trial_id}
                            </TableCell>
                            <TableCell className="text-xs">
                              {sample.queue_key}
                            </TableCell>
                            <TableCell className="font-mono text-xs">
                              {sample.current_worker_id || "—"}
                            </TableCell>
                            <TableCell className="text-xs text-muted-foreground">
                              {formatAge(sample.heartbeat_at)}
                            </TableCell>
                          </TableRow>
                        ))}
                      </TableBody>
                    </Table>
                  )}
                </div>

                <div className="space-y-2">
                  <div className="text-sm font-medium">
                    Task-stage job samples
                  </div>
                  {data.task_samples.length === 0 ? (
                    <p className="text-xs text-muted-foreground">
                      No task samples.
                    </p>
                  ) : (
                    <Table>
                      <TableHeader>
                        <TableRow>
                          <TableHead>Issue</TableHead>
                          <TableHead>Task</TableHead>
                          <TableHead>Status</TableHead>
                          <TableHead>Verdict</TableHead>
                          <TableHead>Updated</TableHead>
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {data.task_samples.map((sample) => (
                          <TableRow key={`${sample.issue}-${sample.task_id}`}>
                            <TableCell className="text-xs">
                              {formatIssueLabel(sample.issue)}
                            </TableCell>
                            <TableCell className="font-mono text-xs">
                              {sample.task_id}
                            </TableCell>
                            <TableCell className="text-xs">
                              {sample.status}
                            </TableCell>
                            <TableCell className="text-xs">
                              {sample.verdict_status || "—"}
                            </TableCell>
                            <TableCell className="text-xs text-muted-foreground">
                              {formatAge(sample.updated_at)}
                            </TableCell>
                          </TableRow>
                        ))}
                      </TableBody>
                    </Table>
                  )}
                </div>
              </>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}


// =============================================================================
// Main Admin Page
// =============================================================================

export default function AdminPage() {
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">Admin Dashboard</h1>
        <p className="text-sm text-muted-foreground">
          Internal system monitoring for workers and job queues
        </p>
      </div>

      <Tabs defaultValue="worker-jobs" className="space-y-4">
        <TabsList>
          <TabsTrigger value="worker-jobs">Worker Jobs</TabsTrigger>
          <TabsTrigger value="concurrency">Concurrency</TabsTrigger>
        </TabsList>

        <TabsContent value="worker-jobs" className="space-y-4">
          <WorkerJobsCard />
          <OrphanedStateCard />
        </TabsContent>

        <TabsContent value="concurrency" className="space-y-4">
          <QueueHealthCard />
          <QueueSlotsCard />
        </TabsContent>
      </Tabs>
    </div>
  );
}
