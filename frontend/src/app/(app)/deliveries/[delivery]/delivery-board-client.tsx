"use client";

import { Fragment, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { usePathname, useSearchParams } from "next/navigation";
import { useAuth } from "@clerk/nextjs";
import useSWR, { SWRConfig, useSWRConfig } from "swr";
import {
  Check,
  ChevronDown,
  ChevronRight,
  Link2,
  Lock,
  Plus,
} from "lucide-react";

import { findingHref } from "@/lib/review";
import { fetcher } from "@/lib/api";
import { formatRelativeTime } from "@/lib/utils";
import {
  parseDeliveryView,
  deliveryViewQuery,
  type DeliveryTaskFilter,
  readySummary,
  deliveryQAStatus,
  isDeliveryBlocked,
  QA_ISSUE_LABELS,
  QA_STATUS_LABELS,
} from "@/lib/deliveries";
import { DeliveryQAWorkEditor } from "@/components/delivery-qa-work-editor";
import { isOrgAdminRole } from "@/lib/org-roles";
import type {
  DeliveryBoardResponse,
  DeliveryCheckResult,
  DeliveryQAStatus,
  QAIssueCategory,
  DeliveryTaskBoardRow,
  TaskBrowseResponse,
  TaskQAHistoryResponse,
} from "@/lib/types";
import { Skeleton } from "@/components/ui/skeleton";
import {
  DeliveryStatusBadge,
  DeliveryQAStatusBadge,
} from "@/components/delivery-status";
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

function postJson<T = void>(url: string, method: string, body?: unknown) {
  return fetcher<T>(url, {
    method,
    headers: body !== undefined ? { "Content-Type": "application/json" } : {},
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
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
        aria-label={check.label}
        disabled={disabled}
        onCheckedChange={(value) => onToggle(value === true)}
        className="mt-0.5"
      />
      <div className="min-w-0">
        <p className="text-sm">
          {check.key === "signoff" && check.status === "pass"
            ? "Sign-off recorded"
            : check.label}
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

/** The board filter. The three non-"all" states are disjoint: every task
 * is blocked (a failing automated check or an open defect), awaiting
 * sign-off (nothing blocks it, a person has not signed it off), or
 * ready. */
function applyTaskFilter(
  tasks: DeliveryTaskBoardRow[],
  filter: DeliveryTaskFilter
) {
  if (filter === "all") return tasks;
  return tasks.filter((row) => {
    if (filter === "outstanding") return !row.ready;
    if (filter === "ready") return row.ready;
    if (filter === "blocked") return isDeliveryBlocked(row);
    return !row.ready && !isDeliveryBlocked(row);
  });
}

function QAHistoryPanel({
  taskId,
  versionId,
  frozen,
}: {
  taskId: string;
  versionId: string | null | undefined;
  frozen: boolean;
}) {
  const { data, error, isValidating, mutate } = useSWR<TaskQAHistoryResponse>(
    `/api/tasks/${encodeURIComponent(taskId)}/qa-history`,
    fetcher,
    { keepPreviousData: true }
  );
  const [showAll, setShowAll] = useState(false);
  const refreshError = error && (
    <div role="alert" className="text-destructive text-xs">
      Failed to refresh QA history: {error.message}. Previously loaded history
      may be out of date.
      <Button
        variant="outline"
        size="sm"
        onClick={() =>
          void mutate(undefined, { populateCache: false, throwOnError: false })
        }
      >
        Retry history
      </Button>
    </div>
  );
  if (!data && error) return refreshError;
  if (!data) {
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
      {refreshError}
      {!error &&
        (isValidating ||
          (!frozen && data.current_version_id !== versionId)) && (
          <p role="status" className="text-muted-foreground text-xs">
            Refreshing history; previously loaded details may be out of date.
          </p>
        )}
      {versions.map((version) => (
        <QAHistoryVersionRow
          key={version.version_id}
          version={version}
          isCurrent={
            frozen ? version.is_current : version.version_id === versionId
          }
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
  isCurrent,
  verdict,
}: {
  isCurrent: boolean;
  version: TaskQAHistoryResponse["versions"][number];
  verdict: TaskQAHistoryResponse["verdict"];
}) {
  return (
    <details className="rounded-md border p-3 text-sm">
      <summary className="cursor-pointer">
        <span className="inline-flex flex-wrap items-center gap-2">
          <span className="font-medium">v{version.version}</span>
          {isCurrent && (
            <span className="bg-secondary rounded-full px-1.5 py-0.5">
              current
            </span>
          )}
          {version.message && (
            <span className="text-muted-foreground">{version.message}</span>
          )}
        </span>
        <span className="text-muted-foreground mt-1 flex flex-wrap gap-x-4 gap-y-1">
          <span>
            source review:{" "}
            {version.pre_trial_status
              ? version.pre_trial_status.toLowerCase()
              : "not run"}
          </span>
          <span>
            rollouts: {version.rollout_count} ({version.rollout_agents} agents)
          </span>
          <span>
            defects: {version.must_fix} requiring resolution or acknowledgment
            {version.pre_trial_should_fix > 0 &&
              ` (${version.pre_trial_should_fix} recorded should_fix in source audit)`}
          </span>
          <span>
            QA runs:{" "}
            {version.qa_runs.length > 0
              ? version.qa_runs
                  .map((run) => `${run.kind} (${run.status ?? "pending"})`)
                  .join(", ")
              : "none"}
          </span>
        </span>
      </summary>
      <div className="mt-3 space-y-3 border-t pt-3">
        {version.pre_trial_error && (
          <p>
            <span className="font-medium text-red-600 dark:text-red-400">
              source review could not complete:
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
        {(version.decisions?.length ?? 0) > 0 && (
          <ul className="space-y-1">
            {version.decisions?.map((decision) => (
              <li key={decision.id}>
                {decision.check_key} · by{" "}
                {decision.checked_by_user_id ?? "unknown person"} for v
                {version.version} ·{" "}
                {new Date(decision.checked_at).toLocaleString()}
                {decision.note && (
                  <p className="text-muted-foreground">{decision.note}</p>
                )}
              </li>
            ))}
          </ul>
        )}
        {!version.pre_trial_error &&
          !version.qa_runs.some((run) => run.error) &&
          verdict == null &&
          version.findings.length === 0 &&
          !version.decisions?.length && (
            <p className="text-muted-foreground">
              No QA details recorded for this version yet.
            </p>
          )}
      </div>
    </details>
  );
}

function TaskRow({
  row,
  frozen,
  isAdmin,
  focused,
  onToggleExpanded,
  link,
  selectable,
  selected,
  onToggleSelect,
  onSetCheck,
  onRemove,
  qa,
  canEditWork,
  busy,
  onClaim,
  onRelease,
  onSaveWork,
}: {
  row: DeliveryTaskBoardRow;
  frozen: boolean;
  isAdmin: boolean;
  // True when the page URL's ?task= names this row: it opens expanded
  // and scrolls into view, so a shared link lands on the right task.
  focused: boolean;
  onToggleExpanded: () => void;
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
  qa: DeliveryQAStatus;
  canEditWork: boolean;
  busy: boolean;
  onClaim: () => void;
  onRelease: () => void;
  onSaveWork: (patch: {
    issue_categories: QAIssueCategory[];
    note: string;
  }) => Promise<void>;
}) {
  const expanded = focused;
  const openDefects = row.defects.filter((defect) => !defect.acknowledged);
  const blocker = openDefects[0];
  const taskHref = `/tasks/${encodeURIComponent(row.task_id)}${row.version != null ? `?version=${row.version}&drawer=task&taskPane=overview` : ""}`;
  const blockerHref =
    blocker && row.version != null
      ? findingHref(row.task_id, row.version, {
          file: blocker.file,
          line_start: blocker.line_start,
          line_end: blocker.line_end,
          id: blocker.finding_id,
        })
      : taskHref;
  const blocked = isDeliveryBlocked(row);
  const [editingWork, setEditingWork] = useState<DeliveryTaskBoardRow | null>(
    null
  );
  const [copied, setCopied] = useState(false);
  const rowRef = useRef<HTMLTableRowElement>(null);
  const manuallyToggled = useRef(false);
  useEffect(() => {
    if (focused && !manuallyToggled.current) {
      rowRef.current?.scrollIntoView({ block: "center" });
    }
    manuallyToggled.current = false;
  }, [focused]);
  const manualChecks = row.checks.filter((check) => check.kind === "manual");
  return (
    <Fragment>
      <TableRow
        ref={rowRef}
        className={`cursor-pointer ${focused ? "bg-secondary/40" : ""}`}
        onClick={() => {
          manuallyToggled.current = true;
          onToggleExpanded();
        }}
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
        <TableCell className="w-10">
          <button
            type="button"
            className="hover:bg-muted flex h-8 w-8 items-center justify-center rounded"
            aria-label={`${expanded ? "Collapse" : "Review"} ${row.task_name}`}
            aria-expanded={expanded}
          >
            {expanded ? (
              <ChevronDown className="h-4 w-4" />
            ) : (
              <ChevronRight className="h-4 w-4" />
            )}
          </button>
        </TableCell>
        <TableCell className="py-4 whitespace-normal">
          <div className="flex items-center gap-2">
            <Link
              href={taskHref}
              title={row.task_name}
              className="min-w-0 truncate text-base font-medium hover:underline"
              onClick={(event) => event.stopPropagation()}
            >
              {row.task_name}
            </Link>
            <button
              type="button"
              className="text-muted-foreground hover:text-foreground inline-flex shrink-0 cursor-pointer"
              aria-label={`Copy link to ${row.task_name}`}
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
                <Check className="h-4 w-4" />
              ) : (
                <Link2 className="h-4 w-4" />
              )}
            </button>
          </div>
          <div className="text-muted-foreground mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-sm">
            <span>
              {row.version != null ? `v${row.version}` : "No version"}
            </span>
            {row.newer_version_exists && <span>Newer version available</span>}
            <DeliveryQAStatusBadge qa={qa} />
            {!row.is_visible && <span>Hidden from customer</span>}
          </div>
          {!expanded && blocker && (
            <a
              href={blockerHref}
              className="text-muted-foreground hover:text-foreground mt-2 block truncate hover:underline"
              onClick={(event) => event.stopPropagation()}
            >
              {blocker.title}
            </a>
          )}
        </TableCell>
        <TableCell className="whitespace-normal">
          <span
            className={
              row.ready
                ? "text-emerald-700 dark:text-emerald-400"
                : blocked
                  ? "text-red-700 dark:text-red-400"
                  : "text-muted-foreground"
            }
          >
            {row.ready
              ? "Ready to deliver"
              : blocked
                ? "Blocked"
                : "Awaiting sign-off"}
          </span>
          {openDefects.length > 0 && (
            <p className="mt-1 text-sm">
              {openDefects.length} finding{openDefects.length === 1 ? "" : "s"}{" "}
              need{openDefects.length === 1 ? "s" : ""} a decision
            </p>
          )}
        </TableCell>
        <TableCell onClick={(event) => event.stopPropagation()}>
          {row.qa_work.owner_user_id ? (
            <div className="space-y-1">
              <p className="text-sm">
                {row.qa_owner_name ?? row.qa_work.owner_user_id}
              </p>
              {canEditWork && (
                <Button
                  variant="ghost"
                  size="sm"
                  disabled={busy}
                  onClick={onRelease}
                >
                  Release
                </Button>
              )}
            </div>
          ) : !frozen && row.version_id ? (
            <Button
              variant="outline"
              size="sm"
              disabled={busy}
              onClick={onClaim}
            >
              Claim
            </Button>
          ) : (
            "Unassigned"
          )}
        </TableCell>
        <TableCell className="text-right">
          <Button
            variant="outline"
            size="sm"
            aria-expanded={expanded}
            onClick={(event) => {
              event.stopPropagation();
              manuallyToggled.current = true;
              onToggleExpanded();
            }}
          >
            {expanded
              ? "Close review"
              : blocked
                ? "Review task"
                : row.ready
                  ? "View record"
                  : "Awaiting sign-off"}
          </Button>
        </TableCell>
      </TableRow>
      {expanded && (
        <TableRow className="hover:bg-transparent">
          {selectable && <TableCell />}
          <TableCell />
          <TableCell colSpan={4} className="pb-6 whitespace-normal">
            <div className="max-w-4xl space-y-3">
              {[false, true].map((acknowledged) => {
                const defects = row.defects.filter(
                  (defect) => defect.acknowledged === acknowledged
                );
                const checks = row.checks.filter(
                  (check) =>
                    check.kind === "automated" &&
                    check.status === (acknowledged ? "waived" : "fail") &&
                    // The individual findings already explain this aggregate check.
                    (check.key !== "no_must_fix" || row.defects.length === 0)
                );
                if (defects.length + checks.length === 0) return null;
                return (
                  <details key={String(acknowledged)} open={!acknowledged}>
                    <summary className="cursor-pointer py-2 text-base font-medium">
                      {acknowledged ? "Acknowledged" : "Needs a decision"}
                      {" · "}
                      {[
                        defects.length > 0
                          ? `${defects.length} finding${defects.length === 1 ? "" : "s"}`
                          : null,
                        checks.length > 0
                          ? `${checks.length} check${checks.length === 1 ? "" : "s"}`
                          : null,
                      ]
                        .filter(Boolean)
                        .join(", ")}
                      {row.version != null && ` · v${row.version}`}
                    </summary>
                    <ul className="divide-y">
                      {defects.map((defect) => (
                        <li
                          key={defect.id}
                          className="grid gap-x-6 gap-y-2 py-4 sm:grid-cols-[minmax(0,1fr)_auto]"
                        >
                          <a
                            className="block max-w-prose text-base leading-relaxed font-medium hover:underline sm:col-start-1"
                            href={
                              row.version != null
                                ? findingHref(row.task_id, row.version, {
                                    file: defect.file,
                                    line_start: defect.line_start,
                                    line_end: defect.line_end,
                                    id: defect.finding_id,
                                  })
                                : taskHref
                            }
                          >
                            {defect.title}
                          </a>
                          <p className="text-muted-foreground text-sm sm:col-start-1">
                            {defect.source === "pre_trial"
                              ? "Source review"
                              : "Execution review"}
                            {defect.recorded_tier &&
                              ` · Recorded severity: ${defect.recorded_tier}`}
                          </p>
                          <details className="text-sm sm:col-start-1">
                            <summary className="cursor-pointer py-1 underline underline-offset-4">
                              {defect.finding
                                ? "Review evidence"
                                : "Finding record"}
                            </summary>
                            <div className="mt-2 max-w-prose space-y-3 leading-relaxed break-words">
                              {defect.finding && (
                                <>
                                  {defect.finding.file && (
                                    <p className="font-mono">
                                      {defect.finding.file}:
                                      {defect.finding.line_start}–
                                      {defect.finding.line_end}
                                    </p>
                                  )}
                                  <p>{defect.finding.detail}</p>
                                  <p>{defect.finding.recommendation}</p>
                                </>
                              )}
                              <p className="text-muted-foreground break-all">
                                Finding: {defect.id}
                                {defect.reporting_trial_id &&
                                  ` · Execution: ${defect.reporting_trial_id}`}
                              </p>
                            </div>
                          </details>
                          {acknowledged ? (
                            <p className="text-muted-foreground text-sm sm:col-start-1">
                              Acknowledged by{" "}
                              {defect.acknowledged_by_name ??
                                defect.acknowledged_by_user_id ??
                                "unknown person"}{" "}
                              for v{row.version}; finding retained
                            </p>
                          ) : (
                            <Button
                              variant="outline"
                              size="sm"
                              className="justify-self-start sm:col-start-2 sm:row-span-3 sm:row-start-1 sm:self-center"
                              disabled={
                                frozen || !isAdmin || busy || !row.version_id
                              }
                              onClick={() =>
                                onSetCheck(
                                  `ack:${defect.id}`,
                                  row.delivery_task_id,
                                  true
                                )
                              }
                            >
                              Acknowledge for v{row.version}
                            </Button>
                          )}
                        </li>
                      ))}
                      {checks.map((check) => (
                        <li key={check.key} className="space-y-3 py-4">
                          <p className="text-base font-medium">
                            {(
                              {
                                pre_trial_passed: "Source review",
                                min_rollouts: "Trial and agent coverage",
                                verdict_ok: "Execution-review verdict",
                                no_must_fix: "Finding decisions",
                              } as Record<string, string>
                            )[check.key] ?? check.label}{" "}
                            ·{" "}
                            {acknowledged
                              ? "Exception acknowledged"
                              : "Requirement unmet"}
                          </p>
                          <p className="max-w-prose text-base leading-relaxed">
                            {check.detail}
                          </p>
                          {acknowledged ? (
                            <p className="text-muted-foreground text-sm">
                              Acknowledged by{" "}
                              {check.checked_by_name ??
                                check.checked_by_user_id ??
                                "unknown person"}{" "}
                              for v{row.version}
                            </p>
                          ) : (
                            check.key !== "no_must_fix" &&
                            check.key !== "task_exists" && (
                              <Button
                                variant="outline"
                                size="sm"
                                disabled={
                                  frozen || !isAdmin || busy || !row.version_id
                                }
                                onClick={() =>
                                  onSetCheck(
                                    `waive:${check.key}`,
                                    row.delivery_task_id,
                                    true
                                  )
                                }
                              >
                                Acknowledge exception for v{row.version}
                              </Button>
                            )
                          )}
                        </li>
                      ))}
                    </ul>
                  </details>
                );
              })}
              {manualChecks.length > 0 && (
                <section
                  className="space-y-2 border-t pt-4"
                  aria-label="Sign-off"
                >
                  <p className="text-base font-medium">
                    Sign-off{row.version != null && ` · v${row.version}`}
                  </p>
                  {manualChecks.map((check) => (
                    <ManualCheckRow
                      key={check.key}
                      check={check}
                      disabled={frozen || !isAdmin || busy}
                      onToggle={(checked) =>
                        onSetCheck(check.key, row.delivery_task_id, checked)
                      }
                    />
                  ))}
                  {blocked &&
                    manualChecks.some(
                      (check) =>
                        check.key === "signoff" && check.status === "pass"
                    ) && (
                      <p className="text-sm">
                        Sign-off is recorded. Outstanding finding or check
                        decisions still block delivery.
                      </p>
                    )}
                </section>
              )}
              {row.qa_work.note && (
                <p className="max-w-prose text-base leading-relaxed whitespace-pre-wrap">
                  {row.qa_work.note}
                </p>
              )}
              {canEditWork && (
                <Button
                  variant="outline"
                  size="sm"
                  disabled={busy}
                  onClick={() => setEditingWork(row)}
                >
                  Edit QA work
                </Button>
              )}
              {editingWork && (
                <DeliveryQAWorkEditor
                  taskName={row.task_name}
                  work={editingWork.qa_work}
                  versionChanged={editingWork.version_id !== row.version_id}
                  onClose={() => setEditingWork(null)}
                  onSave={onSaveWork}
                />
              )}
              <details>
                <summary className="cursor-pointer py-2 text-sm">
                  Review status and checks
                </summary>
                <div className="mt-2 max-w-prose space-y-3 text-sm leading-relaxed">
                  <p>{qa.detail}</p>
                  {qa.finished_at && (
                    <p className="text-muted-foreground">
                      Review finished{" "}
                      {frozen
                        ? new Date(qa.finished_at).toLocaleString()
                        : formatRelativeTime(qa.finished_at)}
                    </p>
                  )}
                  {qa.trial_id && (
                    <Link
                      className="underline"
                      href={`${taskHref}${taskHref.includes("?") ? "&" : "?"}trial=${encodeURIComponent(qa.trial_id)}`}
                    >
                      Open execution-review run
                    </Link>
                  )}
                  {row.checks
                    .filter(
                      (check) =>
                        check.kind === "automated" &&
                        (check.status === "pass" || check.status === "off")
                    )
                    .map((check) => (
                      <p key={check.key}>
                        {check.label} ·{" "}
                        {check.status === "pass" ? "Passed" : "Not required"}
                        <span className="text-muted-foreground block">
                          {check.detail}
                        </span>
                      </p>
                    ))}
                  {row.qa_work.issue_categories.length > 0 && (
                    <p>
                      {row.qa_work.issue_categories
                        .map((key) => QA_ISSUE_LABELS[key])
                        .join(" · ")}
                    </p>
                  )}
                </div>
              </details>
              <details>
                <summary className="cursor-pointer py-2 text-sm">
                  {frozen
                    ? `Live task history · delivery shipped v${row.version}`
                    : "QA history"}
                </summary>
                <QAHistoryPanel
                  taskId={row.task_id}
                  versionId={row.version_id}
                  frozen={frozen}
                />
              </details>
              {isAdmin && !frozen && (
                <details>
                  <summary className="cursor-pointer py-2 text-sm">
                    Task actions
                  </summary>
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
                        <AlertDialogTitle>
                          Remove {row.task_name}?
                        </AlertDialogTitle>
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
                </details>
              )}
            </div>
          </TableCell>
        </TableRow>
      )}
    </Fragment>
  );
}

export type InitialDeliveryBoard = {
  board: DeliveryBoardResponse;
  userId: string;
  orgId: string;
  fetchedAt: number;
};

export function DeliveryBoardClient({
  deliveryId,
  initialBoard,
}: {
  deliveryId: string;
  initialBoard: InitialDeliveryBoard | null;
}) {
  const auth = useAuth();
  // The authenticated server snapshot can render before Clerk hydrates. Once
  // Clerk is ready, its identity decides which cache and data may be shown.
  const userId = auth.isLoaded ? auth.userId : initialBoard?.userId;
  const orgId = auth.isLoaded ? auth.orgId : initialBoard?.orgId;
  const snapshot =
    initialBoard?.userId === userId &&
    initialBoard?.orgId === orgId &&
    initialBoard?.board.delivery.id === deliveryId
      ? initialBoard
      : null;
  return (
    <SWRConfig
      key={JSON.stringify([userId, orgId, deliveryId])}
      value={{ provider: () => new Map() }}
    >
      <DeliveryBoardContent
        deliveryId={deliveryId}
        initialBoard={snapshot?.board ?? null}
        fetchedAt={snapshot?.fetchedAt}
        enabled={Boolean(userId && orgId)}
      />
    </SWRConfig>
  );
}

function DeliveryBoardContent({
  deliveryId,
  initialBoard,
  fetchedAt,
  enabled,
}: {
  deliveryId: string;
  initialBoard: DeliveryBoardResponse | null;
  fetchedAt?: number;
  enabled: boolean;
}) {
  const { mutate: mutateResource } = useSWRConfig();
  const { orgRole } = useAuth();
  const isAdmin = isOrgAdminRole(orgRole);
  const { data, error, mutate } = useSWR<DeliveryBoardResponse>(
    enabled ? `/api/deliveries/${encodeURIComponent(deliveryId)}` : null,
    fetcher,
    {
      revalidateOnMount: !initialBoard,
      refreshInterval: (board) => ((board ?? initialBoard)?.frozen ? 0 : 15000),
      revalidateOnFocus: !initialBoard?.frozen,
      revalidateOnReconnect: !initialBoard?.frozen,
      revalidateIfStale: !initialBoard?.frozen,
      fallbackData: initialBoard ?? undefined,
      onSuccess: (board) => {
        // One polling owner: revalidate only the expanded history after each
        // successful board read, including reads following local mutations.
        const expanded = board.tasks.find(
          (row) => row.task_id === focusTask || row.task_name === focusTask
        );
        if (expanded && !board.frozen) {
          void mutateResource(
            `/api/tasks/${encodeURIComponent(expanded.task_id)}/qa-history`,
            undefined,
            { populateCache: false, throwOnError: false }
          );
        }
      },
    }
  );

  // A router-restored or prefetched snapshot may already be a polling period
  // old. Keep it visible while refreshing; a fresh server load needs no retry.
  useEffect(() => {
    if (
      enabled &&
      initialBoard &&
      !initialBoard.frozen &&
      fetchedAt !== undefined &&
      Date.now() - fetchedAt >= 15000
    ) {
      void mutate(undefined, { populateCache: false, throwOnError: false });
    }
  }, [enabled, initialBoard, fetchedAt, mutate]);

  const [actionError, setActionError] = useState<string | null>(null);
  const [addOpen, setAddOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [signoffConfirm, setSignoffConfirm] =
    useState<DeliveryTaskBoardRow | null>(null);
  const [bulkSignoffRows, setBulkSignoffRows] = useState<
    DeliveryTaskBoardRow[]
  >([]);
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const {
    page,
    filter,
    qaDays,
    qaFilter,
    issueFilter,
    ownerFilter,
    groupBy,
    focusTask,
  } = parseDeliveryView(searchParams);
  function updateView(patch: Parameters<typeof deliveryViewQuery>[1]) {
    // Next integrates native history with useSearchParams. All rows are already
    // loaded, so a view change must not fetch the full board again.
    window.history.pushState(
      null,
      "",
      `${pathname}${deliveryViewQuery(window.location.search, patch)}${window.location.hash}`
    );
  }
  const [notice, setNotice] = useState<string | null>(null);
  // Bulk selection, keyed by delivery_task_id.
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const run = async (action: () => Promise<void>) => {
    setBusy(true);
    setActionError(null);
    try {
      await action();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : "Request failed");
    } finally {
      try {
        await mutate(undefined, { populateCache: false, throwOnError: false });
      } finally {
        setBusy(false);
      }
    }
  };

  const putCheck = (
    checkKey: string,
    deliveryTaskId: string | null,
    checked: boolean,
    expectedVersionId?: string | null
  ) =>
    postJson(
      `/api/deliveries/${encodeURIComponent(deliveryId)}/checks`,
      "PUT",
      {
        check_key: checkKey,
        delivery_task_id: deliveryTaskId,
        checked,
        expected_version_id: expectedVersionId,
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
    const row = data?.tasks.find(
      (row) => row.delivery_task_id === deliveryTaskId
    );
    void run(() =>
      putCheck(
        checkKey,
        deliveryTaskId,
        checked,
        row ? (row.version_id ?? null) : undefined
      )
    );
  };

  const acknowledgeAndSignOff = (row: DeliveryTaskBoardRow) => {
    const { checks, defects } = signoffBlockers(row);
    setSignoffConfirm(null);
    void run(async () => {
      for (const check of checks) {
        await putCheck(
          `waive:${check.key}`,
          row.delivery_task_id,
          true,
          row.version_id ?? null
        );
      }
      for (const defect of defects) {
        await putCheck(
          `ack:${defect.id}`,
          row.delivery_task_id,
          true,
          row.version_id ?? null
        );
      }
      await putCheck(
        "signoff",
        row.delivery_task_id,
        true,
        row.version_id ?? null
      );
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

  const refreshError = error && (
    <div
      role="alert"
      className="text-destructive flex items-center gap-2 text-sm"
    >
      <p>
        Failed to refresh delivery: {error.message}. Previously loaded delivery
        details may be out of date.
      </p>
      <Button
        variant="outline"
        size="sm"
        onClick={() =>
          void mutate(undefined, { populateCache: false, throwOnError: false })
        }
      >
        Retry delivery
      </Button>
    </div>
  );
  if (!data && error) return refreshError;
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
  const cutoff =
    new Date(data.qa_as_of ?? 0).getTime() - Number(qaDays) * 86400000;
  const statuses = new Map(
    data.tasks.map((row) => [
      row.delivery_task_id,
      deliveryQAStatus(row, cutoff),
    ])
  );
  const checkedCount = data.tasks.filter((row) =>
    ["accepted", "needs_fixes"].includes(
      statuses.get(row.delivery_task_id)!.status
    )
  ).length;
  const filteredTasks = applyTaskFilter(data.tasks, filter).filter((row) => {
    const status = statuses.get(row.delivery_task_id)!.status;
    return (
      (qaFilter === "all" ||
        (qaFilter === "checked"
          ? ["accepted", "needs_fixes"].includes(status)
          : qaFilter === "needs_qa"
            ? ["never", "outdated"].includes(status)
            : status === qaFilter)) &&
      (issueFilter === "all" ||
        row.qa_work.issue_categories.includes(
          issueFilter as QAIssueCategory
        )) &&
      (ownerFilter === "all" ||
        (ownerFilter === "unassigned"
          ? !row.qa_work.owner_user_id
          : row.qa_work.owner_user_id === data.qa_viewer_user_id))
    );
  });
  // Resolve IDs before legacy task names, against the complete inventory.
  const focusedTask = focusTask
    ? (data.tasks.find((row) => row.task_id === focusTask) ??
      data.tasks.find((row) => row.task_name === focusTask))
    : undefined;
  const focusOutsideFilters =
    focusedTask != null && !filteredTasks.includes(focusedTask);
  if (focusOutsideFilters) filteredTasks.push(focusedTask);
  const groupLabel = (row: DeliveryTaskBoardRow) =>
    groupBy === "owner"
      ? (row.qa_owner_name ?? row.qa_work.owner_user_id ?? "Unassigned")
      : row.qa_work.issue_categories[0]
        ? QA_ISSUE_LABELS[row.qa_work.issue_categories[0]]
        : "Uncategorized";
  if (groupBy !== "none")
    filteredTasks.sort((a, b) => groupLabel(a).localeCompare(groupLabel(b)));
  const claimWork = (rows: DeliveryTaskBoardRow[], limit: number) =>
    void run(async () => {
      const payload = await postJson<{ claimed_version_ids: string[] }>(
        `/api/deliveries/${encodeURIComponent(deliveryId)}/qa-work/claim`,
        "POST",
        {
          version_ids: rows.flatMap((row) =>
            row.version_id ? [row.version_id] : []
          ),
          limit,
        }
      );
      setNotice(
        `Claimed ${payload.claimed_version_ids.length} tasks. Already assigned tasks were skipped.`
      );
    });
  const patchWork = async (
    row: DeliveryTaskBoardRow,
    patch: {
      release?: boolean;
      issue_categories?: QAIssueCategory[];
      note?: string;
    }
  ) => {
    await postJson(
      `/api/deliveries/${encodeURIComponent(deliveryId)}/qa-work`,
      "PATCH",
      { version_id: row.version_id, ...patch }
    );
  };
  const pageCount = Math.max(
    1,
    Math.ceil(filteredTasks.length / TASK_PAGE_SIZE)
  );
  const focusedIndex = focusedTask ? filteredTasks.indexOf(focusedTask) : -1;
  const clampedPage = Math.min(
    focusedIndex >= 0 ? Math.floor(focusedIndex / TASK_PAGE_SIZE) : page,
    pageCount - 1
  );
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
  const selectedRows = filteredTasks.filter((row) =>
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
  const runnableRows = selectedRows.filter(
    (row) => row.version_id && !["queued", "running"].includes(row.qa.status)
  );
  const rerunSelected = () =>
    void run(async () => {
      const failures: string[] = [];
      let queued = 0;
      for (const row of runnableRows) {
        try {
          await postJson(
            `/api/tasks/${encodeURIComponent(row.task_id)}/qa/retry`,
            "POST",
            {}
          );
          queued += 1;
        } catch (error) {
          failures.push(
            `${row.task_name}: ${error instanceof Error ? error.message : "QA request failed"}`
          );
        }
      }
      setNotice(
        `Requested QA for ${queued} tasks; ${failures.length} failed. Queued and running tasks were skipped.`
      );
      if (failures.length) throw new Error(failures.join("\n"));
    });
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
  const signOffRows = () =>
    void run(async () => {
      for (const row of bulkSignoffRows) {
        await putCheck(
          "signoff",
          row.delivery_task_id,
          true,
          row.version_id ?? null
        );
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
      {refreshError}
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
            <div className="flex flex-wrap items-center gap-2">
              <AddTasksDialog
                open={addOpen}
                onOpenChange={setAddOpen}
                existingTaskIds={new Set(data.tasks.map((row) => row.task_id))}
                busy={busy}
                onAdd={addTasks}
              />
              {cleanUnsigned.length > 0 && (
                <AlertDialog
                  onOpenChange={(open) => {
                    if (open) setBulkSignoffRows(cleanUnsigned);
                  }}
                >
                  <AlertDialogTrigger asChild>
                    <Button variant="outline" size="sm" disabled={busy}>
                      Sign off all ({cleanUnsigned.length})
                    </Button>
                  </AlertDialogTrigger>
                  <AlertDialogContent>
                    <AlertDialogHeader>
                      <AlertDialogTitle>
                        Sign off {bulkSignoffRows.length} task
                        {bulkSignoffRows.length === 1 ? "" : "s"}?
                      </AlertDialogTitle>
                      <AlertDialogDescription>
                        Every check passes on these tasks. Each sign-off is
                        recorded in your name. Tasks with open blockers are not
                        included; sign those off from their row.
                      </AlertDialogDescription>
                    </AlertDialogHeader>
                    <AlertDialogFooter>
                      <AlertDialogCancel>Cancel</AlertDialogCancel>
                      <AlertDialogAction onClick={signOffRows}>
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
              {notice && (
                <p role="status" className="text-muted-foreground mb-3 text-sm">
                  {notice}
                </p>
              )}
              <div className="mb-3 flex flex-wrap items-center gap-2">
                <Select
                  value={filter}
                  onValueChange={(value) => {
                    updateView({ filter: value, page: null, task: null });
                  }}
                >
                  <SelectTrigger className="w-full sm:w-80">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="outstanding">
                      Blockers and outstanding sign-offs
                    </SelectItem>
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
                {bulkable && selectedRows.length > 0 && (
                  <div className="ml-auto flex flex-wrap items-center gap-2">
                    <span className="text-muted-foreground text-sm">
                      {selectedRows.length} selected
                    </span>
                    <Button
                      variant="outline"
                      size="sm"
                      disabled={busy || runnableRows.length === 0}
                      onClick={rerunSelected}
                    >
                      Rerun QA ({runnableRows.length})
                    </Button>
                    <AlertDialog
                      onOpenChange={(open) => {
                        if (open) setBulkSignoffRows(selectedClean);
                      }}
                    >
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
                            Sign off {bulkSignoffRows.length} task
                            {bulkSignoffRows.length === 1 ? "" : "s"}?
                          </AlertDialogTitle>
                          <AlertDialogDescription>
                            {bulkSignoffRows.length} of the{" "}
                            {selectedRows.length} selected tasks have no open
                            blockers and are not signed off; each sign-off is
                            recorded in your name. The rest are skipped — sign
                            those off from their row.
                          </AlertDialogDescription>
                        </AlertDialogHeader>
                        <AlertDialogFooter>
                          <AlertDialogCancel>Cancel</AlertDialogCancel>
                          <AlertDialogAction onClick={signOffRows}>
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
              <details
                className="mb-4"
                open={
                  qaFilter !== "all" ||
                  issueFilter !== "all" ||
                  ownerFilter !== "all" ||
                  groupBy !== "none" ||
                  qaDays !== "7"
                }
              >
                <summary className="cursor-pointer py-2 text-sm">
                  Review filters and grouping
                </summary>
                <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
                  <p className="text-sm">
                    {checkedCount} / {data.tasks.length} tasks checked in the
                    last {qaDays === "7" ? "7 days" : "24 hours"}
                  </p>
                  <Select
                    value={qaDays}
                    onValueChange={(value) => {
                      updateView({ days: value, page: null, task: null });
                    }}
                  >
                    <SelectTrigger
                      className="w-40"
                      aria-label="QA freshness window"
                    >
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="7">Last 7 days</SelectItem>
                      <SelectItem value="1">Last 24 hours</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
                <p className="text-muted-foreground mb-3 text-xs">
                  Checked includes completed reviews with and without blocking
                  defects covering the current version and trials.{" "}
                  {frozen && "Counts are frozen at finalization."}
                </p>
                <div className="mb-3 flex flex-wrap items-center gap-2">
                  <Select
                    value={qaFilter}
                    onValueChange={(value) => {
                      updateView({ qa: value, page: null, task: null });
                    }}
                  >
                    <SelectTrigger
                      className="w-44"
                      aria-label="QA status filter"
                    >
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="all">All QA states</SelectItem>
                      <SelectItem value="checked">Checked</SelectItem>
                      <SelectItem value="needs_qa">Needs QA</SelectItem>
                      {Object.entries(QA_STATUS_LABELS).map(([key, label]) => (
                        <SelectItem key={key} value={key}>
                          {label}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                  <Select
                    value={issueFilter}
                    onValueChange={(value) => {
                      updateView({ issue: value, page: null, task: null });
                    }}
                  >
                    <SelectTrigger
                      className="w-48"
                      aria-label="Issue category filter"
                    >
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="all">All issue categories</SelectItem>
                      {Object.entries(QA_ISSUE_LABELS).map(([key, label]) => (
                        <SelectItem key={key} value={key}>
                          {label}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                  <Select
                    value={ownerFilter}
                    onValueChange={(value) => {
                      updateView({ owner: value, page: null, task: null });
                    }}
                  >
                    <SelectTrigger className="w-40" aria-label="Owner filter">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="all">All owners</SelectItem>
                      <SelectItem value="unassigned">Unassigned</SelectItem>
                      <SelectItem value="mine">Mine</SelectItem>
                    </SelectContent>
                  </Select>
                  <Select
                    value={groupBy}
                    onValueChange={(value) => {
                      updateView({ group: value, page: null, task: null });
                    }}
                  >
                    <SelectTrigger className="w-44" aria-label="Group tasks">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="none">No grouping</SelectItem>
                      <SelectItem value="issue">Group by issue</SelectItem>
                      <SelectItem value="owner">Group by owner</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
              </details>
              {focusOutsideFilters && (
                <p className="text-muted-foreground mb-2 text-xs">
                  The linked task is shown even though it does not match the
                  selected filters.
                </p>
              )}
              {filteredTasks.length === 0 ? (
                <p className="text-muted-foreground text-sm">
                  No tasks match this filter.
                </p>
              ) : (
                <Table className="min-w-[720px] table-fixed">
                  <TableHeader>
                    <TableRow>
                      {bulkable && (
                        <TableHead className="w-8">
                          <Checkbox
                            checked={
                              allFilteredSelected
                                ? true
                                : selectedRows.length > 0
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
                      <TableHead className="w-10" />
                      <TableHead>Task</TableHead>
                      <TableHead className="w-44">Delivery status</TableHead>
                      <TableHead className="w-28">Owner</TableHead>
                      <TableHead className="w-40 text-right">
                        Next action
                      </TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {pagedTasks.map((row, index) => (
                      <Fragment key={row.delivery_task_id}>
                        {groupBy !== "none" &&
                          (index === 0 ||
                            groupLabel(pagedTasks[index - 1]) !==
                              groupLabel(row)) && (
                            <TableRow>
                              <TableCell
                                colSpan={bulkable ? 6 : 5}
                                className="bg-muted text-xs font-medium"
                              >
                                {groupLabel(row)}
                              </TableCell>
                            </TableRow>
                          )}
                        <TaskRow
                          qa={statuses.get(row.delivery_task_id)!}
                          busy={busy}
                          canEditWork={
                            !frozen &&
                            (isAdmin ||
                              (!!data.qa_viewer_user_id &&
                                row.qa_work.owner_user_id ===
                                  data.qa_viewer_user_id))
                          }
                          onClaim={() => claimWork([row], 1)}
                          onRelease={() =>
                            void run(() => patchWork(row, { release: true }))
                          }
                          onSaveWork={async (patch) => {
                            await patchWork(row, patch);
                            await mutate(undefined, {
                              populateCache: false,
                              throwOnError: false,
                            });
                          }}
                          row={row}
                          frozen={frozen}
                          isAdmin={isAdmin}
                          focused={row === focusedTask}
                          onToggleExpanded={() =>
                            updateView({
                              task: row === focusedTask ? null : row.task_id,
                              page: String(clampedPage + 1),
                            })
                          }
                          link={`${pathname}${deliveryViewQuery(searchParams.toString(), { task: row.task_id, page: String(clampedPage + 1) })}`}
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
                      </Fragment>
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
                      onClick={() =>
                        updateView({ page: String(clampedPage), task: null })
                      }
                    >
                      Previous
                    </Button>
                    <Button
                      variant="outline"
                      size="sm"
                      disabled={clampedPage >= pageCount - 1}
                      onClick={() =>
                        updateView({
                          page: String(clampedPage + 2),
                          task: null,
                        })
                      }
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
