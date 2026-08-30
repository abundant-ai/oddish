"use client";

import { Fragment, useState } from "react";
import Link from "next/link";
import { useAuth } from "@clerk/nextjs";
import useSWR from "swr";
import {
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  History,
  Lock,
  Plus,
  XCircle,
} from "lucide-react";

import { fetcher } from "@/lib/api";
import { readySummary } from "@/lib/deliveries";
import { isOrgAdminRole } from "@/lib/org-roles";
import type {
  DeliveryBoardResponse,
  DeliveryCheckResult,
  DeliveryTaskBoardRow,
  TaskQAHistoryResponse,
} from "@/lib/types";
import { CheckChip, DeliveryStatusBadge } from "@/components/delivery-status";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Textarea } from "@/components/ui/textarea";

async function postJson(url: string, method: string, body?: unknown) {
  const res = await fetch(url, {
    method,
    headers: body !== undefined ? { "Content-Type": "application/json" } : {},
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    const payload = (await res.json().catch(() => null)) as {
      detail?: string;
      error?: string;
    } | null;
    throw new Error(
      payload?.detail || payload?.error || `Request failed (${res.status})`,
    );
  }
}

function ManualCheckRow({
  check,
  disabled,
  onToggle,
}: {
  check: DeliveryCheckResult;
  disabled: boolean;
  onToggle: (checked: boolean) => void;
}) {
  return (
    <div className="flex items-start gap-2 py-1">
      <Checkbox
        checked={check.status === "pass"}
        disabled={disabled}
        onCheckedChange={(value) => onToggle(value === true)}
        className="mt-0.5"
      />
      <div className="min-w-0">
        <p className="text-sm">{check.label}</p>
        {check.detail && (
          <p className="text-xs text-muted-foreground">{check.detail}</p>
        )}
      </div>
    </div>
  );
}

function QAHistoryPanel({ taskId }: { taskId: string }) {
  const { data, error, isLoading } = useSWR<TaskQAHistoryResponse>(
    `/api/tasks/${encodeURIComponent(taskId)}/qa-history`,
    fetcher,
  );
  if (error) {
    return (
      <p className="text-xs text-destructive">
        Failed to load QA history: {error.message}
      </p>
    );
  }
  if (isLoading || !data) {
    return <p className="text-xs text-muted-foreground">Loading QA history…</p>;
  }
  return (
    <div className="space-y-2">
      {data.versions.map((version) => (
        <div
          key={version.version_id}
          className="rounded-md border border-[#6f88b4]/20 p-2 text-xs"
        >
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-medium">v{version.version}</span>
            {version.is_current && (
              <span className="rounded-full bg-secondary px-1.5 py-0.5">
                current
              </span>
            )}
            {version.message && (
              <span className="text-muted-foreground">{version.message}</span>
            )}
          </div>
          <div className="mt-1 flex flex-wrap gap-x-4 gap-y-1 text-muted-foreground">
            <span>
              audit:{" "}
              {version.pre_trial_status
                ? version.pre_trial_status.toLowerCase()
                : "not run"}
            </span>
            <span>
              rollouts: {version.rollout_count} ({version.rollout_agents}{" "}
              agents)
            </span>
            <span>
              defects: {version.pre_trial_must_fix} must-fix,{" "}
              {version.pre_trial_should_fix} should-fix
            </span>
            <span>
              QA runs:{" "}
              {version.qa_runs.length > 0
                ? version.qa_runs
                    .map((run) => `${run.kind} (${run.status ?? "pending"})`)
                    .join(", ")
                : "none"}
            </span>
          </div>
        </div>
      ))}
    </div>
  );
}

function TaskRow({
  row,
  frozen,
  isAdmin,
  onSetCheck,
}: {
  row: DeliveryTaskBoardRow;
  frozen: boolean;
  isAdmin: boolean;
  onSetCheck: (
    checkKey: string,
    deliveryTaskId: string,
    checked: boolean,
  ) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const failing = row.checks.filter((check) => check.status === "fail");
  const manualChecks = row.checks.filter((check) => check.kind === "manual");
  return (
    <Fragment>
      <TableRow
        className="cursor-pointer"
        onClick={() => setExpanded((value) => !value)}
      >
        <TableCell className="w-6">
          {expanded ? (
            <ChevronDown className="h-4 w-4 text-muted-foreground" />
          ) : (
            <ChevronRight className="h-4 w-4 text-muted-foreground" />
          )}
        </TableCell>
        <TableCell>
          <Link
            href={`/tasks/${row.task_id}`}
            className="font-medium hover:underline"
            onClick={(event) => event.stopPropagation()}
          >
            {row.task_name}
          </Link>
          {!row.is_visible && (
            <span className="ml-2 text-xs text-muted-foreground">
              (hidden from customer)
            </span>
          )}
        </TableCell>
        <TableCell className="text-muted-foreground">
          {row.version != null ? `v${row.version}` : "—"}
          {row.newer_version_exists && (
            <span
              className="ml-1 text-amber-600 dark:text-amber-400"
              title="A newer version exists that is not the default"
            >
              *
            </span>
          )}
        </TableCell>
        <TableCell>
          <div className="flex flex-wrap gap-1">
            {row.checks.map((check) => (
              <CheckChip key={check.key} check={check} />
            ))}
          </div>
        </TableCell>
        <TableCell className="text-right">
          {row.ready ? (
            <CheckCircle2 className="ml-auto h-4 w-4 text-emerald-600 dark:text-emerald-400" />
          ) : (
            <XCircle className="ml-auto h-4 w-4 text-red-600 dark:text-red-400" />
          )}
        </TableCell>
      </TableRow>
      {expanded && (
        <TableRow className="hover:bg-transparent">
          <TableCell />
          <TableCell colSpan={4} className="space-y-3 py-3">
            {failing.length > 0 && (
              <ul className="space-y-1 text-sm">
                {failing.map((check) => (
                  <li key={check.key} className="flex items-start gap-1.5">
                    <XCircle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-red-600 dark:text-red-400" />
                    <span>
                      <span className="font-medium">{check.label}</span>
                      {check.detail && (
                        <span className="text-muted-foreground">
                          {" "}
                          — {check.detail}
                        </span>
                      )}
                    </span>
                  </li>
                ))}
              </ul>
            )}
            {manualChecks.length > 0 && (
              <div>
                <p className="mb-1 text-xs font-medium uppercase text-muted-foreground">
                  Sign-off
                </p>
                {manualChecks.map((check) => (
                  <ManualCheckRow
                    key={check.key}
                    check={check}
                    disabled={frozen || !isAdmin}
                    onToggle={(checked) =>
                      onSetCheck(check.key, row.delivery_task_id, checked)
                    }
                  />
                ))}
              </div>
            )}
            <div>
              <p className="mb-1 flex items-center gap-1 text-xs font-medium uppercase text-muted-foreground">
                <History className="h-3 w-3" />
                QA history
              </p>
              <QAHistoryPanel taskId={row.task_id} />
            </div>
          </TableCell>
        </TableRow>
      )}
    </Fragment>
  );
}

export function DeliveryBoardClient({ deliveryId }: { deliveryId: string }) {
  const { orgRole } = useAuth();
  const isAdmin = isOrgAdminRole(orgRole);
  const { data, error, isLoading, mutate } = useSWR<DeliveryBoardResponse>(
    `/api/deliveries/${encodeURIComponent(deliveryId)}`,
    fetcher,
    { refreshInterval: 15000 },
  );

  const [actionError, setActionError] = useState<string | null>(null);
  const [addOpen, setAddOpen] = useState(false);
  const [taskIdsText, setTaskIdsText] = useState("");
  const [busy, setBusy] = useState(false);

  const run = async (action: () => Promise<void>) => {
    setBusy(true);
    setActionError(null);
    try {
      await action();
      await mutate();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : "Request failed");
    } finally {
      setBusy(false);
    }
  };

  const setCheck = (
    checkKey: string,
    deliveryTaskId: string | null,
    checked: boolean,
  ) =>
    void run(() =>
      postJson(
        `/api/deliveries/${encodeURIComponent(deliveryId)}/checks`,
        "PUT",
        {
          check_key: checkKey,
          delivery_task_id: deliveryTaskId,
          checked,
        },
      ),
    );

  const addTasks = () => {
    const taskIds = taskIdsText
      .split(/[\s,]+/)
      .map((value) => value.trim())
      .filter(Boolean);
    if (taskIds.length === 0) return;
    void run(async () => {
      await postJson(
        `/api/deliveries/${encodeURIComponent(deliveryId)}/tasks`,
        "POST",
        { task_ids: taskIds },
      );
      setAddOpen(false);
      setTaskIdsText("");
    });
  };

  if (error) {
    return (
      <Card>
        <CardContent className="py-6">
          <p className="text-sm text-destructive">
            Failed to load delivery: {error.message}
          </p>
        </CardContent>
      </Card>
    );
  }
  if (isLoading || !data) {
    return (
      <Card>
        <CardContent className="py-6">
          <p className="text-sm text-muted-foreground">Loading delivery…</p>
        </CardContent>
      </Card>
    );
  }

  const frozen = data.frozen;
  return (
    <div className="space-y-4">
      <Card>
        <CardHeader className="flex flex-row flex-wrap items-center justify-between gap-2">
          <div className="min-w-0">
            <CardTitle className="flex items-center gap-2">
              {data.delivery.name}
              <DeliveryStatusBadge status={data.delivery.status} />
              {frozen && <Lock className="h-4 w-4 text-muted-foreground" />}
            </CardTitle>
            <p className="mt-1 text-sm text-muted-foreground">
              {data.delivery.customer_name
                ? `For ${data.delivery.customer_name} · `
                : ""}
              {readySummary(data)}
              {data.ready && !frozen && " · ready to finalize"}
              {frozen &&
                data.finalized_at &&
                ` · finalized ${new Date(data.finalized_at).toLocaleString()}`}
            </p>
          </div>
          {isAdmin && !frozen && (
            <div className="flex items-center gap-2">
              <Dialog open={addOpen} onOpenChange={setAddOpen}>
                <DialogTrigger asChild>
                  <Button variant="outline" size="sm" disabled={busy}>
                    <Plus className="mr-1 h-4 w-4" />
                    Add tasks
                  </Button>
                </DialogTrigger>
                <DialogContent>
                  <DialogHeader>
                    <DialogTitle>Add tasks</DialogTitle>
                  </DialogHeader>
                  <div className="space-y-1">
                    <Label htmlFor="delivery-task-ids">
                      Task IDs (whitespace or comma separated)
                    </Label>
                    <Textarea
                      id="delivery-task-ids"
                      value={taskIdsText}
                      onChange={(e) => setTaskIdsText(e.target.value)}
                      rows={4}
                    />
                  </div>
                  <DialogFooter>
                    <Button
                      onClick={addTasks}
                      disabled={busy || !taskIdsText.trim()}
                    >
                      Add
                    </Button>
                  </DialogFooter>
                </DialogContent>
              </Dialog>
              <AlertDialog>
                <AlertDialogTrigger asChild>
                  <Button size="sm" disabled={busy || !data.ready}>
                    Finalize
                  </Button>
                </AlertDialogTrigger>
                <AlertDialogContent>
                  <AlertDialogHeader>
                    <AlertDialogTitle>
                      Finalize this delivery?
                    </AlertDialogTitle>
                    <AlertDialogDescription>
                      Finalizing pins every task at its current version and
                      freezes the board as the permanent record of what
                      shipped. A finalized delivery is read-only; follow-up
                      work goes in a new delivery.
                    </AlertDialogDescription>
                  </AlertDialogHeader>
                  <AlertDialogFooter>
                    <AlertDialogCancel>Cancel</AlertDialogCancel>
                    <AlertDialogAction
                      onClick={() =>
                        void run(() =>
                          postJson(
                            `/api/deliveries/${encodeURIComponent(deliveryId)}/finalize`,
                            "POST",
                            {},
                          ),
                        )
                      }
                    >
                      Finalize
                    </AlertDialogAction>
                  </AlertDialogFooter>
                </AlertDialogContent>
              </AlertDialog>
            </div>
          )}
        </CardHeader>
        {(actionError || data.delivery_checks.length > 0) && (
          <CardContent className="space-y-2 pt-0">
            {actionError && (
              <p className="text-sm text-destructive">{actionError}</p>
            )}
            {data.delivery_checks.length > 0 && (
              <div>
                <p className="mb-1 text-xs font-medium uppercase text-muted-foreground">
                  Delivery sign-off
                </p>
                {data.delivery_checks.map((check) => (
                  <ManualCheckRow
                    key={check.key}
                    check={check}
                    disabled={frozen || !isAdmin}
                    onToggle={(checked) => setCheck(check.key, null, checked)}
                  />
                ))}
              </div>
            )}
          </CardContent>
        )}
      </Card>

      <Card>
        <CardContent className="pt-4">
          {data.tasks.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              No tasks yet. Add the tasks this delivery should ship.
            </p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="w-6" />
                  <TableHead>Task</TableHead>
                  <TableHead>Version</TableHead>
                  <TableHead>Checks</TableHead>
                  <TableHead className="text-right">Ready</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {data.tasks.map((row) => (
                  <TaskRow
                    key={row.delivery_task_id}
                    row={row}
                    frozen={frozen}
                    isAdmin={isAdmin}
                    onSetCheck={(checkKey, deliveryTaskId, checked) =>
                      setCheck(checkKey, deliveryTaskId, checked)
                    }
                  />
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
