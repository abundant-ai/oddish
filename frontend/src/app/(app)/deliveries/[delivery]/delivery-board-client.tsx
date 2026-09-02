"use client";

import { Fragment, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useAuth } from "@clerk/nextjs";
import useSWR, { preload } from "swr";
import {
  AlertCircle,
  Check,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  History,
  Link2,
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
  TaskBrowseResponse,
  TaskQAHistoryResponse,
} from "@/lib/types";
import { Skeleton } from "@/components/ui/skeleton";
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
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
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
      payload?.detail || payload?.error || `Request failed (${res.status})`
    );
  }
}

/** What still blocks a sign-off: failing automated checks that need a
 * waive, and defects without an acknowledgement. */
function signoffBlockers(row: DeliveryTaskBoardRow) {
  const checks = row.checks.filter(
    (check) =>
      check.kind === "automated" &&
      check.status === "fail" &&
      check.key !== "no_must_fix" &&
      check.key !== "task_exists"
  );
  const defects = row.defects.filter((defect) => !defect.acknowledged);
  return { checks, defects };
}

function AddTasksDialog({
  open,
  onOpenChange,
  existingTaskIds,
  busy,
  onAdd,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  existingTaskIds: Set<string>;
  busy: boolean;
  onAdd: (taskIds: string[]) => void;
}) {
  const [mode, setMode] = useState<"search" | "paste">("search");
  const [search, setSearch] = useState("");
  const [query, setQuery] = useState("");
  const [pasteText, setPasteText] = useState("");
  const [selected, setSelected] = useState<Map<string, string>>(new Map());

  useEffect(() => {
    const handle = setTimeout(() => setQuery(search.trim()), 300);
    return () => clearTimeout(handle);
  }, [search]);

  const { data, error, isLoading } = useSWR<TaskBrowseResponse>(
    open && mode === "search"
      ? `/api/tasks/browse?q=${encodeURIComponent(query)}`
      : null,
    fetcher,
    { keepPreviousData: true }
  );
  const results = (data?.items ?? []).filter(
    (item) => !existingTaskIds.has(item.id)
  );

  const toggle = (id: string, name: string) => {
    setSelected((current) => {
      const next = new Map(current);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.set(id, name);
      }
      return next;
    });
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(value) => {
        onOpenChange(value);
        if (!value) {
          setSelected(new Map());
          setSearch("");
          setPasteText("");
          setMode("search");
        }
      }}
    >
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
        <div className="flex items-center gap-1">
          <Button
            variant={mode === "search" ? "secondary" : "ghost"}
            size="sm"
            onClick={() => setMode("search")}
          >
            Search
          </Button>
          <Button
            variant={mode === "paste" ? "secondary" : "ghost"}
            size="sm"
            onClick={() => setMode("paste")}
          >
            Paste list
          </Button>
          {mode === "search" && results.length > 1 && (
            <Button
              variant="ghost"
              size="sm"
              className="ml-auto"
              onClick={() =>
                setSelected((current) => {
                  const next = new Map(current);
                  for (const item of results) {
                    next.set(item.id, item.name);
                  }
                  return next;
                })
              }
            >
              Select all {results.length}
            </Button>
          )}
        </div>
        {mode === "paste" ? (
          <Textarea
            autoFocus
            value={pasteText}
            onChange={(e) => setPasteText(e.target.value)}
            rows={6}
            placeholder={
              "One task name or id per line.\nCommas and spaces also work."
            }
          />
        ) : (
          <Input
            autoFocus
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search tasks by name…"
          />
        )}
        {mode === "paste" ? null : (
          <div className="max-h-64 space-y-0.5 overflow-y-auto">
            {error ? (
              <p className="text-destructive py-2 text-sm">
                Search failed: {error.message}
              </p>
            ) : isLoading && !data ? (
              <div className="space-y-1 py-1">
                <Skeleton className="h-7 w-full" />
                <Skeleton className="h-7 w-full" />
                <Skeleton className="h-7 w-3/4" />
              </div>
            ) : results.length === 0 ? (
              <p className="text-muted-foreground py-2 text-sm">
                {query
                  ? "No matching tasks (or they are already in this delivery)."
                  : "Type to search your tasks."}
              </p>
            ) : (
              results.map((item) => (
                <label
                  key={item.id}
                  className="hover:bg-muted flex cursor-pointer items-center gap-2 rounded-md px-2 py-1.5"
                >
                  <Checkbox
                    checked={selected.has(item.id)}
                    onCheckedChange={() => toggle(item.id, item.name)}
                  />
                  <span className="min-w-0 flex-1 truncate text-sm">
                    {item.name}
                  </span>
                  {item.current_version != null && (
                    <span className="text-muted-foreground text-xs">
                      v{item.current_version}
                    </span>
                  )}
                </label>
              ))
            )}
          </div>
        )}
        <DialogFooter>
          {mode === "paste" ? (
            <Button
              onClick={() =>
                onAdd(
                  pasteText
                    .split(/[\s,]+/)
                    .map((ref) => ref.trim())
                    .filter(Boolean)
                )
              }
              disabled={busy || !pasteText.trim()}
            >
              Add pasted tasks
            </Button>
          ) : (
            <Button
              onClick={() => onAdd([...selected.keys()])}
              disabled={busy || selected.size === 0}
            >
              {selected.size > 0
                ? `Add ${selected.size} task${selected.size === 1 ? "" : "s"}`
                : "Add"}
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
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
        <p className="text-sm">
          {check.label}
          {check.status === "pass" && check.checked_by_user_id && (
            <span className="text-muted-foreground">
              {" "}
              · by {check.checked_by_name ?? check.checked_by_user_id}
            </span>
          )}
        </p>
        {check.detail && (
          <p className="text-muted-foreground text-xs">{check.detail}</p>
        )}
      </div>
    </div>
  );
}

// Versions listed before "Show all" expands the history.
const QA_HISTORY_PAGE = 5;
// Task rows per page on the board.
const TASK_PAGE_SIZE = 25;

type TaskFilter = "all" | "blocked" | "awaiting_signoff" | "ready";

/** The board filter. The three non-"all" states are disjoint: every task
 * is blocked (a failing automated check or an open defect), awaiting
 * sign-off (nothing blocks it, a person has not signed it off), or
 * ready. */
function applyTaskFilter(tasks: DeliveryTaskBoardRow[], filter: TaskFilter) {
  if (filter === "all") return tasks;
  const isBlocked = (row: DeliveryTaskBoardRow) =>
    row.checks.some(
      (check) => check.kind === "automated" && check.status === "fail"
    ) || row.defects.some((defect) => !defect.acknowledged);
  return tasks.filter((row) => {
    if (filter === "ready") return row.ready;
    if (filter === "blocked") return isBlocked(row);
    return !row.ready && !isBlocked(row);
  });
}

function QAHistoryPanel({ taskId }: { taskId: string }) {
  const { data, error, isLoading } = useSWR<TaskQAHistoryResponse>(
    `/api/tasks/${encodeURIComponent(taskId)}/qa-history`,
    fetcher,
    { keepPreviousData: true }
  );
  const [showAll, setShowAll] = useState(false);
  if (error) {
    return (
      <p className="text-destructive text-xs">
        Failed to load QA history: {error.message}
      </p>
    );
  }
  if (isLoading || !data) {
    return (
      <div className="space-y-2">
        <Skeleton className="h-10 w-full" />
        <Skeleton className="h-10 w-2/3" />
      </div>
    );
  }
  const versions = showAll
    ? data.versions
    : data.versions.slice(0, QA_HISTORY_PAGE);
  const unversioned = data.unversioned_runs ?? [];
  return (
    <div className="space-y-2">
      {versions.map((version) => (
        <QAHistoryVersionRow
          key={version.version_id}
          version={version}
          verdict={
            version.version_id === data.verdict_version_id
              ? (data.verdict ?? null)
              : null
          }
        />
      ))}
      {data.versions.length > QA_HISTORY_PAGE && !showAll && (
        <button
          type="button"
          className="text-muted-foreground cursor-pointer text-xs hover:underline"
          onClick={() => setShowAll(true)}
        >
          Show all {data.versions.length} versions
        </button>
      )}
      {unversioned.length > 0 && (
        <p className="text-muted-foreground text-xs">
          Runs not tied to a version:{" "}
          {unversioned
            .map((run) => `${run.kind} (${run.status ?? "pending"})`)
            .join(", ")}
        </p>
      )}
    </div>
  );
}

function QAHistoryVersionRow({
  version,
  verdict,
}: {
  version: TaskQAHistoryResponse["versions"][number];
  verdict: TaskQAHistoryResponse["verdict"];
}) {
  const [open, setOpen] = useState(false);
  return (
    <div className="rounded-md border border-[#6f88b4]/20 p-2 text-xs">
      <button
        type="button"
        className="w-full cursor-pointer text-left"
        onClick={() => setOpen((value) => !value)}
      >
        <div className="flex flex-wrap items-center gap-2">
          {open ? (
            <ChevronDown className="text-muted-foreground h-3 w-3" />
          ) : (
            <ChevronRight className="text-muted-foreground h-3 w-3" />
          )}
          <span className="font-medium">v{version.version}</span>
          {version.is_current && (
            <span className="bg-secondary rounded-full px-1.5 py-0.5">
              current
            </span>
          )}
          {version.message && (
            <span className="text-muted-foreground">{version.message}</span>
          )}
        </div>
        <div className="text-muted-foreground mt-1 flex flex-wrap gap-x-4 gap-y-1">
          <span>
            audit:{" "}
            {version.pre_trial_status
              ? version.pre_trial_status.toLowerCase()
              : "not run"}
          </span>
          <span>
            rollouts: {version.rollout_count} ({version.rollout_agents} agents)
          </span>
          <span>
            defects: {version.must_fix} must-fix, {version.pre_trial_should_fix}{" "}
            should-fix
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
      </button>
      {open && (
        <div className="mt-2 space-y-2 border-t border-[#6f88b4]/20 pt-2">
          {version.pre_trial_error && (
            <p>
              <span className="font-medium text-red-600 dark:text-red-400">
                audit failed:
              </span>{" "}
              <span className="text-muted-foreground break-words">
                {version.pre_trial_error}
              </span>
            </p>
          )}
          {version.qa_runs.some((run) => run.error) && (
            <ul className="space-y-1">
              {version.qa_runs
                .filter((run) => run.error)
                .map((run) => (
                  <li key={run.trial_id}>
                    <span className="font-medium text-red-600 dark:text-red-400">
                      {run.kind} {run.status?.toLowerCase() ?? ""}:
                    </span>{" "}
                    <span className="text-muted-foreground break-words">
                      {run.error}
                    </span>
                  </li>
                ))}
            </ul>
          )}
          {verdict != null && (
            <div>
              <span
                className={
                  verdict.is_good
                    ? "font-medium text-emerald-600 dark:text-emerald-400"
                    : "font-medium text-red-600 dark:text-red-400"
                }
              >
                verdict:{" "}
                {verdict.verdict ?? (verdict.is_good ? "accept" : "reject")}
              </span>
              {verdict.primary_issue && (
                <p className="text-muted-foreground mt-0.5">
                  {verdict.primary_issue}
                </p>
              )}
              {verdict.reasoning && (
                <p className="text-muted-foreground mt-0.5">
                  {verdict.reasoning}
                </p>
              )}
            </div>
          )}
          {version.findings.length > 0 && (
            <ul className="space-y-1">
              {version.findings.map((finding, index) => (
                <li key={index} className="flex items-start gap-2">
                  <span
                    className={`shrink-0 rounded-full px-1.5 py-0.5 ${
                      finding.tier === "must_fix"
                        ? "bg-red-500/15 text-red-700 dark:text-red-400"
                        : "bg-muted text-muted-foreground"
                    }`}
                  >
                    {finding.tier.replace("_", "-") || "note"}
                  </span>
                  <span className="min-w-0">
                    {finding.title}
                    {finding.source === "trial" && (
                      <span className="text-muted-foreground">
                        {" "}
                        (from a trial)
                      </span>
                    )}
                  </span>
                </li>
              ))}
            </ul>
          )}
          {!version.pre_trial_error &&
            !version.qa_runs.some((run) => run.error) &&
            verdict == null &&
            version.findings.length === 0 && (
              <p className="text-muted-foreground">
                No QA details recorded for this version yet.
              </p>
            )}
        </div>
      )}
    </div>
  );
}

function TaskRow({
  row,
  frozen,
  isAdmin,
  focused,
  link,
  selectable,
  selected,
  onToggleSelect,
  onSetCheck,
  onRemove,
}: {
  row: DeliveryTaskBoardRow;
  frozen: boolean;
  isAdmin: boolean;
  // True when the page URL's ?task= names this row: it opens expanded
  // and scrolls into view, so a shared link lands on the right task.
  focused: boolean;
  link: string;
  // Bulk selection: admins get a checkbox per row while the delivery is
  // not frozen; the selection drives the bulk action bar above the table.
  selectable: boolean;
  selected: boolean;
  onToggleSelect: () => void;
  onSetCheck: (
    checkKey: string,
    deliveryTaskId: string,
    checked: boolean
  ) => void;
  onRemove: () => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const [copied, setCopied] = useState(false);
  const rowRef = useRef<HTMLTableRowElement>(null);
  useEffect(() => {
    if (focused) {
      setExpanded(true);
      rowRef.current?.scrollIntoView({ block: "center" });
    }
  }, [focused]);
  // Manual checks live in the sign-off section below; listing them here
  // too would say the same thing twice.
  const failing = row.checks.filter(
    (check) =>
      check.kind === "automated" &&
      (check.status === "fail" || check.status === "waived")
  );
  const manualChecks = row.checks.filter((check) => check.kind === "manual");
  return (
    <Fragment>
      <TableRow
        ref={rowRef}
        className={`cursor-pointer ${focused ? "bg-secondary/40" : ""}`}
        onClick={() => setExpanded((value) => !value)}
      >
        {selectable && (
          <TableCell
            className="w-8"
            onClick={(event) => event.stopPropagation()}
          >
            <Checkbox
              checked={selected}
              onCheckedChange={() => onToggleSelect()}
              aria-label={`Select ${row.task_name}`}
            />
          </TableCell>
        )}
        <TableCell className="w-6">
          {expanded ? (
            <ChevronDown className="text-muted-foreground h-4 w-4" />
          ) : (
            <ChevronRight className="text-muted-foreground h-4 w-4" />
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
          <button
            type="button"
            className="text-muted-foreground hover:text-foreground ml-2 inline-flex cursor-pointer align-middle"
            title="Copy a link to this task"
            onClick={(event) => {
              event.stopPropagation();
              void navigator.clipboard.writeText(
                `${window.location.origin}${link}`
              );
              setCopied(true);
              setTimeout(() => setCopied(false), 1500);
            }}
          >
            {copied ? (
              <Check className="h-3.5 w-3.5 text-emerald-600 dark:text-emerald-400" />
            ) : (
              <Link2 className="h-3.5 w-3.5" />
            )}
          </button>
          {!row.is_visible && (
            <span className="text-muted-foreground ml-2 text-xs">
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
          {selectable && <TableCell />}
          <TableCell />
          <TableCell colSpan={4} className="space-y-3 py-3">
            {failing.length > 0 && (
              <ul className="space-y-1 text-sm">
                {failing.map((check) => (
                  <li
                    key={check.key}
                    className="flex flex-wrap items-center gap-1.5"
                  >
                    {check.status === "waived" ? (
                      <AlertCircle className="h-3.5 w-3.5 shrink-0 text-amber-600 dark:text-amber-400" />
                    ) : (
                      <XCircle className="h-3.5 w-3.5 shrink-0 text-red-600 dark:text-red-400" />
                    )}
                    <span className="min-w-0 flex-1">
                      <span className="font-medium">{check.label}</span>
                      {check.detail && (
                        <span className="text-muted-foreground">
                          {" "}
                          — {check.detail}
                        </span>
                      )}
                    </span>
                    {check.status === "waived" ? (
                      <span className="text-muted-foreground text-xs">
                        acknowledged by{" "}
                        {check.checked_by_name ?? check.checked_by_user_id}
                      </span>
                    ) : // no_must_fix takes per-defect acks; task_exists is
                    // the deleted-task marker — neither is waivable.
                    check.key === "no_must_fix" ||
                      check.key === "task_exists" ? null : (
                      <Button
                        variant="outline"
                        size="sm"
                        disabled={frozen || !isAdmin}
                        onClick={(event) => {
                          event.stopPropagation();
                          onSetCheck(
                            `waive:${check.key}`,
                            row.delivery_task_id,
                            true
                          );
                        }}
                      >
                        Acknowledge
                      </Button>
                    )}
                  </li>
                ))}
              </ul>
            )}
            {row.defects.length > 0 && (
              <div>
                <p className="text-muted-foreground mb-1 text-xs font-medium uppercase">
                  Defects
                </p>
                <ul className="space-y-1 text-sm">
                  {row.defects.map((defect) => (
                    <li
                      key={defect.id}
                      className="flex flex-wrap items-center gap-2"
                    >
                      <span className="text-muted-foreground font-mono text-xs">
                        {defect.id}
                      </span>
                      <span className="min-w-0 flex-1 truncate">
                        {defect.title}
                      </span>
                      {defect.acknowledged ? (
                        <span className="text-muted-foreground text-xs">
                          acknowledged by{" "}
                          {defect.acknowledged_by_name ??
                            defect.acknowledged_by_user_id}
                        </span>
                      ) : (
                        <Button
                          variant="outline"
                          size="sm"
                          disabled={frozen || !isAdmin}
                          onClick={(event) => {
                            event.stopPropagation();
                            onSetCheck(
                              `ack:${defect.id}`,
                              row.delivery_task_id,
                              true
                            );
                          }}
                        >
                          Acknowledge
                        </Button>
                      )}
                    </li>
                  ))}
                </ul>
              </div>
            )}
            {manualChecks.length > 0 && (
              <div>
                <p className="text-muted-foreground mb-1 text-xs font-medium uppercase">
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
              <p className="text-muted-foreground mb-1 flex items-center gap-1 text-xs font-medium uppercase">
                <History className="h-3 w-3" />
                QA history
              </p>
              <QAHistoryPanel taskId={row.task_id} />
            </div>
            {isAdmin && !frozen && (
              <AlertDialog>
                <AlertDialogTrigger asChild>
                  <Button
                    variant="outline"
                    size="sm"
                    className="text-destructive"
                  >
                    Remove from delivery
                  </Button>
                </AlertDialogTrigger>
                <AlertDialogContent>
                  <AlertDialogHeader>
                    <AlertDialogTitle>Remove {row.task_name}?</AlertDialogTitle>
                    <AlertDialogDescription>
                      The task leaves this delivery. Its sign-off and
                      acknowledgements go with it. The task itself is not
                      deleted.
                    </AlertDialogDescription>
                  </AlertDialogHeader>
                  <AlertDialogFooter>
                    <AlertDialogCancel>Cancel</AlertDialogCancel>
                    <AlertDialogAction onClick={onRemove}>
                      Remove
                    </AlertDialogAction>
                  </AlertDialogFooter>
                </AlertDialogContent>
              </AlertDialog>
            )}
          </TableCell>
        </TableRow>
      )}
    </Fragment>
  );
}

export function DeliveryBoardClient({
  deliveryId,
  initialBoard,
}: {
  deliveryId: string;
  initialBoard: DeliveryBoardResponse | null;
}) {
  const { orgRole } = useAuth();
  const isAdmin = isOrgAdminRole(orgRole);
  const { data, error, mutate } = useSWR<DeliveryBoardResponse>(
    `/api/deliveries/${encodeURIComponent(deliveryId)}`,
    fetcher,
    { refreshInterval: 15000, fallbackData: initialBoard ?? undefined }
  );

  const [actionError, setActionError] = useState<string | null>(null);
  const [addOpen, setAddOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [signoffConfirm, setSignoffConfirm] =
    useState<DeliveryTaskBoardRow | null>(null);
  // ?task=<name or id> deep-links one row: it expands, scrolls into view,
  // and its page is selected. Read from location so the page needs no
  // Suspense boundary for useSearchParams.
  const [focusTask, setFocusTask] = useState<string | null>(null);
  const [page, setPage] = useState(0);
  const [filter, setFilter] = useState<TaskFilter>("all");
  // Bulk selection, keyed by delivery_task_id.
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const prefetched = useRef(new Set<string>());
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    setFocusTask(params.get("task"));
    const filterParam = params.get("filter");
    if (
      filterParam === "blocked" ||
      filterParam === "awaiting_signoff" ||
      filterParam === "ready"
    ) {
      setFilter(filterParam);
    }
    const pageParam = Number(params.get("page"));
    if (Number.isInteger(pageParam) && pageParam >= 1) {
      setPage(pageParam - 1);
    }
  }, []);
  // Filter and page live in the URL (?filter=, ?page=, 1-based), so a view
  // can be shared or reloaded. replaceState keeps the back button out of
  // every click; defaults stay out of the URL.
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    if (filter === "all") {
      params.delete("filter");
    } else {
      params.set("filter", filter);
    }
    if (page === 0) {
      params.delete("page");
    } else {
      params.set("page", String(page + 1));
    }
    const query = params.toString();
    const next = `${window.location.pathname}${query ? `?${query}` : ""}`;
    if (next !== `${window.location.pathname}${window.location.search}`) {
      window.history.replaceState(null, "", next);
    }
  }, [filter, page]);
  // Land the ?task= deep link once: pick its page in the filtered view
  // the table actually renders. When ?filter= hides the task, the link
  // wins and the filter falls back to all tasks.
  const focusHandled = useRef(false);
  useEffect(() => {
    if (!data || !focusTask || focusHandled.current) return;
    focusHandled.current = true;
    const matches = (row: DeliveryTaskBoardRow) =>
      row.task_name === focusTask || row.task_id === focusTask;
    const index = applyTaskFilter(data.tasks, filter).findIndex(matches);
    if (index >= 0) {
      setPage(Math.floor(index / TASK_PAGE_SIZE));
      return;
    }
    const unfiltered = data.tasks.findIndex(matches);
    if (unfiltered >= 0) {
      setFilter("all");
      setPage(Math.floor(unfiltered / TASK_PAGE_SIZE));
    }
  }, [data, focusTask, filter]);
  // Fetch the visible rows' QA history as soon as the board is up, so
  // expanding a row shows it without a loading wait.
  useEffect(() => {
    if (!data) return;
    const rows = applyTaskFilter(data.tasks, filter);
    const start =
      Math.min(page, Math.max(0, Math.ceil(rows.length / TASK_PAGE_SIZE) - 1)) *
      TASK_PAGE_SIZE;
    for (const row of rows.slice(start, start + TASK_PAGE_SIZE)) {
      const key = `/api/tasks/${encodeURIComponent(row.task_id)}/qa-history`;
      if (!prefetched.current.has(key)) {
        prefetched.current.add(key);
        void preload(key, fetcher);
      }
    }
  }, [data, page, filter]);

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

  const putCheck = (
    checkKey: string,
    deliveryTaskId: string | null,
    checked: boolean
  ) =>
    postJson(
      `/api/deliveries/${encodeURIComponent(deliveryId)}/checks`,
      "PUT",
      {
        check_key: checkKey,
        delivery_task_id: deliveryTaskId,
        checked,
      }
    );

  const setCheck = (
    checkKey: string,
    deliveryTaskId: string | null,
    checked: boolean
  ) => {
    // Ticking sign-off on a task with open blockers needs an explicit
    // confirmation; the dialog lists them and acknowledges on confirm.
    if (checkKey === "signoff" && checked && data) {
      const row = data.tasks.find((r) => r.delivery_task_id === deliveryTaskId);
      if (row) {
        const { checks, defects } = signoffBlockers(row);
        if (checks.length + defects.length > 0) {
          setSignoffConfirm(row);
          return;
        }
      }
    }
    void run(() => putCheck(checkKey, deliveryTaskId, checked));
  };

  const acknowledgeAndSignOff = (row: DeliveryTaskBoardRow) => {
    const { checks, defects } = signoffBlockers(row);
    setSignoffConfirm(null);
    void run(async () => {
      for (const check of checks) {
        await putCheck(`waive:${check.key}`, row.delivery_task_id, true);
      }
      for (const defect of defects) {
        await putCheck(`ack:${defect.id}`, row.delivery_task_id, true);
      }
      await putCheck("signoff", row.delivery_task_id, true);
    });
  };

  const removeTask = (taskId: string) =>
    void run(() =>
      postJson(
        `/api/deliveries/${encodeURIComponent(deliveryId)}/tasks/${encodeURIComponent(taskId)}`,
        "DELETE"
      )
    );

  const addTasks = (taskIds: string[]) => {
    if (taskIds.length === 0) return;
    void run(async () => {
      await postJson(
        `/api/deliveries/${encodeURIComponent(deliveryId)}/tasks`,
        "POST",
        { task_ids: taskIds }
      );
      setAddOpen(false);
    });
  };

  if (error) {
    return (
      <Card>
        <CardContent className="py-6">
          <p className="text-destructive text-sm">
            Failed to load delivery: {error.message}
          </p>
        </CardContent>
      </Card>
    );
  }
  if (!data) {
    return (
      <div className="space-y-4">
        <Card>
          <CardContent className="space-y-2 py-6">
            <Skeleton className="h-6 w-64" />
            <Skeleton className="h-4 w-40" />
          </CardContent>
        </Card>
        <Card>
          <CardContent className="space-y-2 py-6">
            <Skeleton className="h-8 w-full" />
            <Skeleton className="h-8 w-full" />
            <Skeleton className="h-8 w-full" />
          </CardContent>
        </Card>
      </div>
    );
  }

  const frozen = data.frozen;
  const filteredTasks = applyTaskFilter(data.tasks, filter);
  const pageCount = Math.max(
    1,
    Math.ceil(filteredTasks.length / TASK_PAGE_SIZE)
  );
  const clampedPage = Math.min(page, pageCount - 1);
  const pagedTasks = filteredTasks.slice(
    clampedPage * TASK_PAGE_SIZE,
    (clampedPage + 1) * TASK_PAGE_SIZE
  );
  // Tasks a mass sign-off may take: not signed off, no open blockers.
  // Blocked tasks keep the per-task acknowledge flow.
  const cleanUnsigned = data.tasks.filter((row) => {
    const signoffCheck = row.checks.find((check) => check.key === "signoff");
    if (!signoffCheck || signoffCheck.status === "pass") return false;
    const { checks, defects } = signoffBlockers(row);
    return checks.length + defects.length === 0;
  });
  const bulkable = isAdmin && !frozen;
  const selectedRows = data.tasks.filter((row) =>
    selected.has(row.delivery_task_id)
  );
  const cleanUnsignedIds = new Set(
    cleanUnsigned.map((row) => row.delivery_task_id)
  );
  // Sign off selected takes only the clean, unsigned part of the
  // selection; blocked tasks keep the per-task acknowledge flow.
  const selectedClean = selectedRows.filter((row) =>
    cleanUnsignedIds.has(row.delivery_task_id)
  );
  const allFilteredSelected =
    filteredTasks.length > 0 &&
    filteredTasks.every((row) => selected.has(row.delivery_task_id));
  const toggleSelect = (deliveryTaskId: string) =>
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(deliveryTaskId)) {
        next.delete(deliveryTaskId);
      } else {
        next.add(deliveryTaskId);
      }
      return next;
    });
  const signOffSelected = () =>
    void run(async () => {
      for (const row of selectedClean) {
        await putCheck("signoff", row.delivery_task_id, true);
      }
      setSelected(new Set());
    });
  const removeSelected = () =>
    void run(async () => {
      for (const row of selectedRows) {
        await postJson(
          `/api/deliveries/${encodeURIComponent(deliveryId)}/tasks/${encodeURIComponent(row.task_id)}`,
          "DELETE"
        );
      }
      setSelected(new Set());
    });
  return (
    <div className="space-y-4">
      <Card>
        <CardHeader className="flex flex-row flex-wrap items-center justify-between gap-2">
          <div className="min-w-0">
            <CardTitle className="flex items-center gap-2">
              {data.delivery.name}
              <DeliveryStatusBadge status={data.delivery.status} />
              {frozen && <Lock className="text-muted-foreground h-4 w-4" />}
            </CardTitle>
            <p className="text-muted-foreground mt-1 text-sm">
              Customer:{" "}
              <span className="text-foreground font-medium">
                {data.delivery.customer_name ?? "not set"}
              </span>
              {" · "}
              {readySummary(data)}
              {data.ready && !frozen && " · ready to finalize"}
              {frozen &&
                data.finalized_at &&
                ` · finalized ${new Date(data.finalized_at).toLocaleString()}`}
            </p>
          </div>
          {isAdmin && !frozen && (
            <div className="flex items-center gap-2">
              <AddTasksDialog
                open={addOpen}
                onOpenChange={setAddOpen}
                existingTaskIds={new Set(data.tasks.map((row) => row.task_id))}
                busy={busy}
                onAdd={addTasks}
              />
              {cleanUnsigned.length > 0 && (
                <AlertDialog>
                  <AlertDialogTrigger asChild>
                    <Button variant="outline" size="sm" disabled={busy}>
                      Sign off all ({cleanUnsigned.length})
                    </Button>
                  </AlertDialogTrigger>
                  <AlertDialogContent>
                    <AlertDialogHeader>
                      <AlertDialogTitle>
                        Sign off {cleanUnsigned.length} task
                        {cleanUnsigned.length === 1 ? "" : "s"}?
                      </AlertDialogTitle>
                      <AlertDialogDescription>
                        Every check passes on these tasks. Each sign-off is
                        recorded in your name. Tasks with open blockers are not
                        included; sign those off from their row.
                      </AlertDialogDescription>
                    </AlertDialogHeader>
                    <AlertDialogFooter>
                      <AlertDialogCancel>Cancel</AlertDialogCancel>
                      <AlertDialogAction
                        onClick={() =>
                          void run(async () => {
                            for (const row of cleanUnsigned) {
                              await putCheck(
                                "signoff",
                                row.delivery_task_id,
                                true
                              );
                            }
                          })
                        }
                      >
                        Sign off all
                      </AlertDialogAction>
                    </AlertDialogFooter>
                  </AlertDialogContent>
                </AlertDialog>
              )}
              <AlertDialog>
                <AlertDialogTrigger asChild>
                  <Button size="sm" disabled={busy || !data.ready}>
                    Finalize
                  </Button>
                </AlertDialogTrigger>
                <AlertDialogContent>
                  <AlertDialogHeader>
                    <AlertDialogTitle>Finalize this delivery?</AlertDialogTitle>
                    <AlertDialogDescription>
                      Finalizing pins every task at its current version and
                      freezes the board as the permanent record of what shipped.
                      A finalized delivery is read-only; follow-up work goes in
                      a new delivery.
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
                            {}
                          )
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
              <p className="text-destructive text-sm">{actionError}</p>
            )}
            {data.delivery_checks.length > 0 && (
              <div>
                <p className="text-muted-foreground mb-1 text-xs font-medium uppercase">
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
            <p className="text-muted-foreground text-sm">
              No tasks yet. Add the tasks this delivery should ship.
            </p>
          ) : (
            <>
              <div className="mb-3 flex flex-wrap items-center gap-2">
                <Select
                  value={filter}
                  onValueChange={(value) => {
                    setFilter(value as TaskFilter);
                    setPage(0);
                  }}
                >
                  <SelectTrigger className="w-64">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="all">All tasks</SelectItem>
                    <SelectItem value="blocked">
                      Blocked (failing checks or defects)
                    </SelectItem>
                    <SelectItem value="awaiting_signoff">
                      Awaiting sign-off (checks pass)
                    </SelectItem>
                    <SelectItem value="ready">Ready</SelectItem>
                  </SelectContent>
                </Select>
                {filter !== "all" && (
                  <span className="text-muted-foreground text-sm">
                    {filteredTasks.length} of {data.tasks.length} tasks
                  </span>
                )}
                {bulkable && selected.size > 0 && (
                  <div className="ml-auto flex flex-wrap items-center gap-2">
                    <span className="text-muted-foreground text-sm">
                      {selected.size} selected
                    </span>
                    <AlertDialog>
                      <AlertDialogTrigger asChild>
                        <Button
                          variant="outline"
                          size="sm"
                          disabled={busy || selectedClean.length === 0}
                        >
                          Sign off selected ({selectedClean.length})
                        </Button>
                      </AlertDialogTrigger>
                      <AlertDialogContent>
                        <AlertDialogHeader>
                          <AlertDialogTitle>
                            Sign off {selectedClean.length} task
                            {selectedClean.length === 1 ? "" : "s"}?
                          </AlertDialogTitle>
                          <AlertDialogDescription>
                            {selectedClean.length} of the {selected.size}{" "}
                            selected tasks have no open blockers and are not
                            signed off; each sign-off is recorded in your name.
                            The rest are skipped — sign those off from their
                            row.
                          </AlertDialogDescription>
                        </AlertDialogHeader>
                        <AlertDialogFooter>
                          <AlertDialogCancel>Cancel</AlertDialogCancel>
                          <AlertDialogAction onClick={signOffSelected}>
                            Sign off
                          </AlertDialogAction>
                        </AlertDialogFooter>
                      </AlertDialogContent>
                    </AlertDialog>
                    <AlertDialog>
                      <AlertDialogTrigger asChild>
                        <Button
                          variant="outline"
                          size="sm"
                          className="text-destructive"
                          disabled={busy}
                        >
                          Remove selected ({selected.size})
                        </Button>
                      </AlertDialogTrigger>
                      <AlertDialogContent>
                        <AlertDialogHeader>
                          <AlertDialogTitle>
                            Remove {selected.size} task
                            {selected.size === 1 ? "" : "s"}?
                          </AlertDialogTitle>
                          <AlertDialogDescription>
                            The tasks leave this delivery. Their sign-offs and
                            acknowledgements go with them. The tasks themselves
                            are not deleted.
                          </AlertDialogDescription>
                        </AlertDialogHeader>
                        <AlertDialogFooter>
                          <AlertDialogCancel>Cancel</AlertDialogCancel>
                          <AlertDialogAction onClick={removeSelected}>
                            Remove
                          </AlertDialogAction>
                        </AlertDialogFooter>
                      </AlertDialogContent>
                    </AlertDialog>
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => setSelected(new Set())}
                    >
                      Clear
                    </Button>
                  </div>
                )}
              </div>
              {filteredTasks.length === 0 ? (
                <p className="text-muted-foreground text-sm">
                  No tasks match this filter.
                </p>
              ) : (
                <Table>
                  <TableHeader>
                    <TableRow>
                      {bulkable && (
                        <TableHead className="w-8">
                          <Checkbox
                            checked={
                              allFilteredSelected
                                ? true
                                : selected.size > 0
                                  ? "indeterminate"
                                  : false
                            }
                            onCheckedChange={(value) =>
                              setSelected(
                                value === true
                                  ? new Set(
                                      filteredTasks.map(
                                        (row) => row.delivery_task_id
                                      )
                                    )
                                  : new Set()
                              )
                            }
                            aria-label="Select all tasks in this view"
                          />
                        </TableHead>
                      )}
                      <TableHead className="w-6" />
                      <TableHead>Task</TableHead>
                      <TableHead>Version</TableHead>
                      <TableHead>Checks</TableHead>
                      <TableHead className="text-right">Ready</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {pagedTasks.map((row) => (
                      <TaskRow
                        key={row.delivery_task_id}
                        row={row}
                        frozen={frozen}
                        isAdmin={isAdmin}
                        focused={
                          focusTask === row.task_name ||
                          focusTask === row.task_id
                        }
                        link={`/deliveries/${encodeURIComponent(deliveryId)}?task=${encodeURIComponent(row.task_name)}`}
                        selectable={bulkable}
                        selected={selected.has(row.delivery_task_id)}
                        onToggleSelect={() =>
                          toggleSelect(row.delivery_task_id)
                        }
                        onSetCheck={(checkKey, deliveryTaskId, checked) =>
                          setCheck(checkKey, deliveryTaskId, checked)
                        }
                        onRemove={() => removeTask(row.task_id)}
                      />
                    ))}
                  </TableBody>
                </Table>
              )}
              {pageCount > 1 && (
                <div className="text-muted-foreground mt-3 flex items-center justify-between text-sm">
                  <span>
                    Page {clampedPage + 1} of {pageCount} ·{" "}
                    {filteredTasks.length} tasks
                  </span>
                  <div className="flex gap-2">
                    <Button
                      variant="outline"
                      size="sm"
                      disabled={clampedPage === 0}
                      onClick={() => setPage(clampedPage - 1)}
                    >
                      Previous
                    </Button>
                    <Button
                      variant="outline"
                      size="sm"
                      disabled={clampedPage >= pageCount - 1}
                      onClick={() => setPage(clampedPage + 1)}
                    >
                      Next
                    </Button>
                  </div>
                </div>
              )}
            </>
          )}
        </CardContent>
      </Card>
      <AlertDialog
        open={signoffConfirm !== null}
        onOpenChange={(open) => {
          if (!open) setSignoffConfirm(null);
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              This task does not meet the requirements
            </AlertDialogTitle>
            <AlertDialogDescription asChild>
              <div className="space-y-2">
                {signoffConfirm && (
                  <ul className="list-disc space-y-1 pl-5 text-sm">
                    {signoffBlockers(signoffConfirm).checks.map((check) => (
                      <li key={check.key}>
                        {check.label}
                        {check.detail ? ` — ${check.detail}` : ""}
                      </li>
                    ))}
                    {signoffBlockers(signoffConfirm).defects.map((defect) => (
                      <li key={defect.id}>
                        defect {defect.id} — {defect.title}
                      </li>
                    ))}
                  </ul>
                )}
                <p>
                  Sign off anyway? Each item gets an acknowledgement recorded in
                  your name.
                </p>
              </div>
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => {
                if (signoffConfirm) acknowledgeAndSignOff(signoffConfirm);
              }}
            >
              Acknowledge and sign off
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
