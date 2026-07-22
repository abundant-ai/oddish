"use client";

import {
  Suspense,
  use,
  useEffect,
  useRef,
  useState,
  useTransition,
} from "react";
import useSWR from "swr";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Alert, AlertTitle, AlertDescription } from "@/components/ui/alert";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { ExperimentsSkeleton } from "./experiments-skeleton";
import type {
  DashboardExperiment,
  DashboardExperimentAuthor,
  OrgUser,
} from "@/lib/types";
import { fetcher } from "@/lib/api";
import {
  SearchSyntaxHelp,
  SearchSyntaxMultiRow,
  SearchSyntaxRow,
} from "@/components/search-syntax-help";
import { TagChip } from "@/components/tag-chip";
import { TagFilterDropdown } from "@/components/tag-filter-dropdown";
import {
  cn,
  encodeExperimentRouteParam,
  formatShortDateTime,
  prBadge,
} from "@/lib/utils";
import {
  DASHBOARD_DEFAULT_EXPERIMENTS_AUTHOR,
  DASHBOARD_DEFAULT_EXPERIMENTS_LIMIT,
} from "@/lib/dashboard-request";
import { badgeVariants } from "@/components/ui/badge";
import { UsageSummaryCard } from "@/components/usage-overview";
import { QuotaUsageCard } from "@/components/quota-usage-card";
import { CostLeaderboardStrip } from "@/components/cost-leaderboard";
import {
  ArrowRight,
  Check,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  Clock,
  Copy,
  ExternalLink,
  Filter,
  GitPullRequest,
  Trash2,
  Globe,
  Key,
  Terminal,
  Users,
  X,
} from "lucide-react";

// =============================================================================
// Experiments list — server-fetched, streamed via Suspense
// =============================================================================

const EXPERIMENTS_PAGE_SIZE = DASHBOARD_DEFAULT_EXPERIMENTS_LIMIT;
const STATUS_FILTER_OPTIONS = [
  { value: "all", label: "All statuses" },
  { value: "active", label: "Active trials" },
  { value: "retrying", label: "Retrying trials" },
  { value: "completed", label: "Completed" },
  { value: "needs-review", label: "Needs review" },
  { value: "pending-verdict", label: "QA pending" },
  { value: "failed", label: "Failures" },
] as const;

// Resolved by the server-side experiments-only fetch (see dashboard/page.tsx).
// `ok` is false when the upstream fetch failed so the body can surface an error
// instead of an empty state.
export type ExperimentsResult = {
  experiments: DashboardExperiment[];
  hasMore: boolean;
  ok: boolean;
};

function formatTaskAuthor(author: DashboardExperimentAuthor | null): string {
  if (!author) return "—";
  // Precedence: canonical org-member name (plain, no "@") -> @github handle ->
  // raw owner/user string. "member" is resolved by the backend from the
  // experiment's owner_user_id and matches the name shown on the cost page.
  if (author.source === "member") {
    return author.name;
  }
  if (author.source === "github") {
    return `@${author.name.replace(/^@/, "")}`;
  }
  return author.name;
}

function memberDisplayName(member: OrgUser): string {
  return member.name || member.github_username || member.email;
}

// Org member roster for the experiments owner filter. The backend gates
// GET /users on admin/owner role, so non-admins simply get an empty list
// and fall back to the Org / Mine toggle. Errors are swallowed for the
// same reason -- the picker is progressive enhancement, not required.
function useOrgMembers(): OrgUser[] {
  const { data } = useSWR<OrgUser[]>("/api/users", fetcher, {
    revalidateOnFocus: false,
    shouldRetryOnError: false,
  });
  return Array.isArray(data) ? data : [];
}

function CommandSnippet({ command }: { command: string }) {
  const [copied, setCopied] = useState(false);

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(command);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      setCopied(false);
    }
  };

  return (
    <div className="border-border/80 bg-muted/35 flex items-center gap-2 rounded-md border px-3 py-2">
      <code className="min-w-0 flex-1 overflow-x-auto font-mono text-xs">
        {command}
      </code>
      <Button
        type="button"
        variant="ghost"
        size="sm"
        className="h-7 px-2 text-xs"
        onClick={handleCopy}
        aria-label="Copy command"
      >
        {copied ? (
          <Check className="h-3.5 w-3.5 text-[#5c8e43]" />
        ) : (
          <Copy className="h-3.5 w-3.5" />
        )}
      </Button>
    </div>
  );
}

function EmptyExperimentsState() {
  return (
    <div className="bg-card/60 rounded-lg border border-dashed border-[#6f88b4]/30 p-6">
      <div className="flex flex-col items-center text-center">
        <Clock className="text-muted-foreground/70 mb-3 h-11 w-11" />
        <p className="text-base font-medium">No experiments yet</p>
      </div>

      <div className="mt-5 grid gap-3 lg:grid-cols-3">
        <div className="bg-background/80 rounded-lg border border-[#85b85c]/20 p-4">
          <div className="mb-3 flex items-center gap-2 text-sm font-medium">
            <Terminal className="h-4 w-4 text-[#5c8e43]" />
            Install the CLI
          </div>
          <CommandSnippet command="uv pip install oddish" />
        </div>

        <div className="bg-background/80 rounded-lg border border-[#6f88b4]/20 p-4">
          <div className="mb-3 flex items-center gap-2 text-sm font-medium">
            <Key className="h-4 w-4 text-[#6f88b4]" />
            Add an API key
          </div>
          <CommandSnippet command={'export ODDISH_API_KEY="ok_..."'} />
        </div>

        <div className="bg-background/80 rounded-lg border border-[#85b85c]/20 p-4">
          <div className="mb-3 flex items-center gap-2 text-sm font-medium">
            <ArrowRight className="h-4 w-4 text-[#5c8e43]" />
            Submit your first job
          </div>
          <CommandSnippet command="oddish run -p my-task -a codex -m openai/gpt-5.4" />
        </div>
      </div>
    </div>
  );
}

function MineEmptyExperimentsState({
  onViewOrgExperiments,
}: {
  onViewOrgExperiments: () => void;
}) {
  return (
    <div className="bg-card/60 rounded-lg border border-dashed border-[#6f88b4]/30 p-6">
      <div className="flex flex-col items-center text-center">
        <Users className="text-muted-foreground/70 mb-3 h-11 w-11" />
        <p className="text-base font-medium">No experiments of yours yet</p>
        <p className="text-muted-foreground mt-1 max-w-md text-sm">
          Your organization may have other experiments. Switch to the org view
          to browse everything, or submit a new job to get started.
        </p>
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="mt-4"
          onClick={onViewOrgExperiments}
        >
          View org experiments
        </Button>
      </div>
    </div>
  );
}

// =============================================================================
// Experiments table body — unwraps the server promise inside Suspense
// =============================================================================

function ExperimentsTableBody({
  promise,
  authorFilter,
  statusFilter,
  searchQuery,
  currentExperimentsPage,
  onPreviousExperimentsPage,
  onNextExperimentsPage,
  onRefreshData,
  onViewOrgExperiments,
}: {
  promise: Promise<ExperimentsResult>;
  authorFilter: string;
  statusFilter: string;
  searchQuery: string;
  currentExperimentsPage: number;
  onPreviousExperimentsPage: () => void;
  onNextExperimentsPage: () => void;
  onRefreshData: () => void;
  onViewOrgExperiments: () => void;
}) {
  const { experiments, hasMore, ok } = use(promise);

  const [deleteTarget, setDeleteTarget] = useState<{
    id: string;
    name: string;
    taskCount: number;
    totalTrials: number;
  } | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [isDeleting, setIsDeleting] = useState(false);

  const isMemberSelected = authorFilter !== "all" && authorFilter !== "me";
  const hasFilters =
    searchQuery.trim().length > 0 || statusFilter !== "all" || isMemberSelected;

  const handleDeleteExperiment = async () => {
    if (!deleteTarget || isDeleting) return;
    setIsDeleting(true);
    setDeleteError(null);

    try {
      const res = await fetch(
        `/api/experiments/${encodeExperimentRouteParam(deleteTarget.id)}`,
        { method: "DELETE" }
      );

      if (!res.ok) {
        const errorData = await res.json().catch(() => ({}));
        throw new Error(
          errorData.detail || errorData.error || "Failed to delete experiment"
        );
      }

      onRefreshData();
      setDeleteTarget(null);
    } catch (error) {
      setDeleteError(
        error instanceof Error ? error.message : "Failed to delete experiment"
      );
    } finally {
      setIsDeleting(false);
    }
  };

  return (
    <>
      <div className="text-muted-foreground mb-3 text-[11px]">
        Showing {experiments.length}
        {" • "}
        Page {currentExperimentsPage}
      </div>
      {!ok && experiments.length === 0 ? (
        <Alert variant="destructive">
          <AlertTitle>Failed to load experiments</AlertTitle>
          <AlertDescription>
            Check the API connection and try again.
          </AlertDescription>
        </Alert>
      ) : experiments.length === 0 && !hasMore && !hasFilters ? (
        authorFilter === "me" ? (
          <MineEmptyExperimentsState
            onViewOrgExperiments={onViewOrgExperiments}
          />
        ) : (
          <EmptyExperimentsState />
        )
      ) : experiments.length === 0 ? (
        <div className="text-muted-foreground py-8 text-center">
          <p>No experiments match the current filters.</p>
        </div>
      ) : (
        <div className="max-h-[68vh] min-h-[560px] overflow-y-auto">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Experiment</TableHead>
                <TableHead>Author</TableHead>
                <TableHead>Last run</TableHead>
                <TableHead>PR</TableHead>
                <TableHead>Tasks</TableHead>
                <TableHead>Trials</TableHead>
                <TableHead
                  className="cursor-help"
                  title="Average of per-task average reward, nop/oracle excluded"
                >
                  Avg score
                </TableHead>
                <TableHead className="text-right">Last task</TableHead>
                <TableHead className="text-right">Delete</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody className="[&_td]:text-xs">
              {experiments.map((experiment) => {
                const avgScorePct =
                  experiment.avg_score != null
                    ? Math.round(experiment.avg_score * 100)
                    : null;
                const retryingTrials = Number(experiment.retrying_trials) || 0;

                return (
                  <TableRow key={experiment.id}>
                    <TableCell>
                      <div className="flex items-center gap-1.5">
                        <Link
                          href={`/experiments/${encodeExperimentRouteParam(
                            experiment.id
                          )}`}
                          className="text-[#5d77a5] transition-colors hover:text-[#526a95] dark:text-[#a8b8d2] dark:hover:text-[#c0cde1]"
                        >
                          {experiment.name}
                        </Link>
                        {experiment.is_public && (
                          <Globe
                            className="text-muted-foreground h-3.5 w-3.5"
                            aria-label="Published experiment"
                          />
                        )}
                      </div>
                      {(experiment.user_tags?.length ?? 0) > 0 && (
                        <div className="mt-0.5 flex flex-wrap items-center gap-1">
                          {experiment.user_tags!.map((t) => (
                            <TagChip key={t.tag_id} tag={t} />
                          ))}
                        </div>
                      )}
                    </TableCell>
                    <TableCell className="text-muted-foreground text-xs whitespace-nowrap">
                      <span className="text-foreground/80">
                        {formatTaskAuthor(
                          experiment.author ?? experiment.last_author
                        )}
                      </span>
                    </TableCell>
                    <TableCell className="text-muted-foreground text-xs whitespace-nowrap">
                      <span className="text-foreground/80">
                        {formatTaskAuthor(
                          experiment.last_runner ?? experiment.last_author
                        )}
                      </span>
                    </TableCell>
                    <TableCell className="text-xs whitespace-nowrap">
                      {experiment.last_pr_url ? (
                        <Link
                          href={experiment.last_pr_url}
                          target="_blank"
                          rel="noreferrer"
                          title={
                            experiment.last_pr_title
                              ? `${experiment.last_pr_title} — view on GitHub`
                              : "View pull request on GitHub"
                          }
                          className={cn(
                            badgeVariants({ variant: "outline" }),
                            "hover:bg-accent max-w-[200px] gap-1.5 font-mono text-[11px] transition-colors"
                          )}
                        >
                          <GitPullRequest
                            className="h-3 w-3 shrink-0"
                            aria-hidden
                          />
                          {(() => {
                            const { label, number } = prBadge(
                              experiment.last_pr_url,
                              experiment.last_pr_number
                            );
                            return (
                              <span className="min-w-0 truncate">
                                {label}
                                {number && (
                                  <span className="text-muted-foreground">
                                    {" "}
                                    #{number}
                                  </span>
                                )}
                              </span>
                            );
                          })()}
                          <ExternalLink
                            className="h-3 w-3 shrink-0 opacity-50"
                            aria-hidden
                          />
                        </Link>
                      ) : (
                        <span className="text-muted-foreground">—</span>
                      )}
                    </TableCell>
                    <TableCell>{experiment.task_count}</TableCell>
                    <TableCell className="font-mono text-xs whitespace-nowrap">
                      {/* done = terminal (success + failed + skipped), so a
                          finished experiment reads N/N, not "2/5". The (R)/(F)/(S)
                          suffixes break down the composition. */}
                      {experiment.completed_trials +
                        experiment.failed_trials +
                        experiment.skipped_trials}
                      /{experiment.total_trials}
                      {retryingTrials > 0 && (
                        <span className="text-amber-500 dark:text-amber-300">
                          {" "}
                          ({retryingTrials}R)
                        </span>
                      )}
                      {experiment.failed_trials > 0 && (
                        <span className="text-rose-400">
                          {" "}
                          ({experiment.failed_trials}F)
                        </span>
                      )}
                      {experiment.skipped_trials > 0 && (
                        <span className="text-muted-foreground">
                          {" "}
                          ({experiment.skipped_trials}S)
                        </span>
                      )}
                    </TableCell>
                    <TableCell className="font-mono text-xs">
                      {avgScorePct === null ? (
                        <span className="text-muted-foreground">—</span>
                      ) : (
                        <span
                          className={
                            avgScorePct >= 80
                              ? "text-[#5c8e43] dark:text-[#85b85c]"
                              : avgScorePct >= 35
                                ? "text-yellow-400"
                                : "text-rose-400"
                          }
                        >
                          {avgScorePct}%
                        </span>
                      )}
                    </TableCell>
                    <TableCell className="text-muted-foreground text-right text-xs whitespace-nowrap">
                      {experiment.last_created_at
                        ? formatShortDateTime(experiment.last_created_at)
                        : "—"}
                    </TableCell>
                    <TableCell className="text-right">
                      <Button
                        variant="ghost"
                        size="icon"
                        onClick={() =>
                          setDeleteTarget({
                            id: experiment.id,
                            name: experiment.name,
                            taskCount: experiment.task_count,
                            totalTrials: experiment.total_trials,
                          })
                        }
                        disabled={
                          experiment.id === "uncategorized" ||
                          experiment.name === "Uncategorized"
                        }
                        className="text-destructive hover:text-destructive h-8 w-8"
                        aria-label={`Delete ${experiment.name}`}
                      >
                        <Trash2 className="h-4 w-4" />
                      </Button>
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
          <div className="mt-3 flex items-center gap-2">
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="h-8 px-3 text-[11px]"
              onClick={onPreviousExperimentsPage}
              disabled={currentExperimentsPage <= 1}
            >
              <ChevronLeft className="mr-1 h-3.5 w-3.5" />
              Previous page
            </Button>
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="h-8 px-3 text-[11px]"
              onClick={onNextExperimentsPage}
              disabled={!hasMore}
            >
              Next page
              <ChevronRight className="ml-1 h-3.5 w-3.5" />
            </Button>
          </div>
        </div>
      )}
      <AlertDialog
        open={Boolean(deleteTarget)}
        onOpenChange={(open) => {
          if (!open) {
            setDeleteTarget(null);
            setDeleteError(null);
          }
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete this experiment?</AlertDialogTitle>
            <AlertDialogDescription>
              This permanently deletes{" "}
              <span className="text-foreground font-medium">
                {deleteTarget?.name}
              </span>{" "}
              and removes {deleteTarget?.taskCount ?? 0} tasks and{" "}
              {deleteTarget?.totalTrials ?? 0} trials. This action cannot be
              undone.
            </AlertDialogDescription>
          </AlertDialogHeader>
          {deleteError && (
            <Alert variant="destructive">
              <AlertTitle>Delete failed</AlertTitle>
              <AlertDescription>{deleteError}</AlertDescription>
            </Alert>
          )}
          <AlertDialogFooter>
            <AlertDialogCancel disabled={isDeleting}>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={handleDeleteExperiment}
              disabled={isDeleting}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              {isDeleting ? "Deleting..." : "Delete experiment"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}

// =============================================================================
// Recent Experiments card — filter controls + Suspense-wrapped table
// =============================================================================

function RecentTasksCard({
  experimentsPromise,
  paramsKey,
  searchQuery,
  onSearchQueryChange,
  statusFilter,
  onStatusFilterChange,
  authorFilter,
  onAuthorFilterChange,
  members,
  currentExperimentsPage,
  onPreviousExperimentsPage,
  onNextExperimentsPage,
  onRefreshData,
  onViewOrgExperiments,
}: {
  experimentsPromise: Promise<ExperimentsResult>;
  paramsKey: string;
  searchQuery: string;
  onSearchQueryChange: (value: string) => void;
  statusFilter: string;
  onStatusFilterChange: (value: string) => void;
  authorFilter: string;
  onAuthorFilterChange: (value: string) => void;
  members: OrgUser[];
  currentExperimentsPage: number;
  onPreviousExperimentsPage: () => void;
  onNextExperimentsPage: () => void;
  onRefreshData: () => void;
  onViewOrgExperiments: () => void;
}) {
  const isMemberSelected = authorFilter !== "all" && authorFilter !== "me";
  const statusFilterLabel =
    STATUS_FILTER_OPTIONS.find((option) => option.value === statusFilter)
      ?.label ?? "Filter status";
  const selectedMember = isMemberSelected
    ? members.find((member) => member.email === authorFilter)
    : undefined;
  const memberFilterLabel = selectedMember
    ? memberDisplayName(selectedMember)
    : "Members";

  return (
    <Card className="col-span-5 border-[#6f88b4]/20 shadow-xs">
      <CardHeader className="flex flex-col gap-3 pb-3 sm:flex-row sm:items-center sm:justify-between">
        <div className="space-y-1">
          <CardTitle className="text-base">Recent Experiments</CardTitle>
        </div>
        <div className="flex flex-1 flex-wrap gap-2 sm:justify-end">
          {/* Owner filter: Org / Mine toggle + optional member picker. */}
          <div className="flex items-center gap-0.5 rounded-md border border-[#6f88b4]/20 p-0.5">
            <Button
              type="button"
              variant={authorFilter === "all" ? "secondary" : "ghost"}
              size="sm"
              className="h-7 px-2.5 text-[11px]"
              onClick={() => onAuthorFilterChange("all")}
              aria-pressed={authorFilter === "all"}
            >
              Org
            </Button>
            <Button
              type="button"
              variant={authorFilter === "me" ? "secondary" : "ghost"}
              size="sm"
              className="h-7 px-2.5 text-[11px]"
              onClick={() => onAuthorFilterChange("me")}
              aria-pressed={authorFilter === "me"}
            >
              Mine
            </Button>
          </div>
          {members.length > 0 && (
            <DropdownMenu modal={false}>
              <DropdownMenuTrigger asChild>
                <Button
                  type="button"
                  variant={isMemberSelected ? "secondary" : "outline"}
                  size="sm"
                  className="h-8 w-full justify-between border-[#6f88b4]/20 sm:w-[180px]"
                >
                  <span className="flex min-w-0 items-center gap-1.5">
                    <Users className="h-3.5 w-3.5 shrink-0 opacity-60" />
                    <span className="truncate">{memberFilterLabel}</span>
                  </span>
                  <ChevronDown className="h-4 w-4 opacity-50" />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent
                align="end"
                className="max-h-[320px] w-[240px] overflow-y-auto"
              >
                <DropdownMenuRadioGroup
                  value={isMemberSelected ? authorFilter : ""}
                  onValueChange={onAuthorFilterChange}
                >
                  {members.map((member) => (
                    <DropdownMenuRadioItem key={member.id} value={member.email}>
                      <span className="flex min-w-0 flex-col">
                        <span className="truncate">
                          {memberDisplayName(member)}
                        </span>
                        {member.github_username && (
                          <span className="text-muted-foreground truncate text-[10px]">
                            @{member.github_username}
                          </span>
                        )}
                      </span>
                    </DropdownMenuRadioItem>
                  ))}
                </DropdownMenuRadioGroup>
              </DropdownMenuContent>
            </DropdownMenu>
          )}
          <div className="relative w-full sm:w-[220px]">
            <Input
              value={searchQuery}
              onChange={(event) => onSearchQueryChange(event.target.value)}
              placeholder="Search anything..."
              className="h-8 w-full border-[#6f88b4]/20 pr-7"
            />
            <SearchSyntaxHelp>
              <p className="font-medium">Search syntax</p>
              <p className="text-muted-foreground">
                Matches experiment name, author, or tags. Add a prefix below to
                specify filters.
              </p>
              <SearchSyntaxRow
                example="cybersecurity agent"
                hint="every word must match (AND)"
              />
              <SearchSyntaxRow
                example="daytona OR modal"
                hint="either word (OR)"
              />
              <SearchSyntaxRow example={'"exact name"'} hint="exact phrase" />
              <SearchSyntaxRow example="-wip" hint="exclude" />
              <SearchSyntaxMultiRow
                examples={["github:alice", "author:alice", "user:alice"]}
                hint="by author — GitHub handle, email, or name"
              />
              <SearchSyntaxRow example="tag:smoke" hint="by a specific tag" />
              <p className="text-muted-foreground">
                Filters stack (AND) and are case-insensitive, e.g.{" "}
                <code className="bg-muted rounded px-1 font-mono">
                  github:alice tag:smoke
                </code>
              </p>
            </SearchSyntaxHelp>
          </div>
          <TagFilterDropdown
            query={searchQuery}
            onQueryChange={onSearchQueryChange}
            countField="experiment_count"
          />
          <DropdownMenu modal={false}>
            <DropdownMenuTrigger asChild>
              <Button
                type="button"
                variant="outline"
                size="sm"
                className="h-8 w-full justify-between border-[#6f88b4]/20 sm:w-[170px]"
              >
                <span className="truncate">{statusFilterLabel}</span>
                <ChevronDown className="h-4 w-4 opacity-50" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="w-[170px]">
              <DropdownMenuRadioGroup
                value={statusFilter}
                onValueChange={onStatusFilterChange}
              >
                {STATUS_FILTER_OPTIONS.map((option) => (
                  <DropdownMenuRadioItem
                    key={option.value}
                    value={option.value}
                  >
                    {option.label}
                  </DropdownMenuRadioItem>
                ))}
              </DropdownMenuRadioGroup>
            </DropdownMenuContent>
          </DropdownMenu>
          <ExperimentTrialFilters />
        </div>
      </CardHeader>
      <CardContent>
        <Suspense key={paramsKey} fallback={<ExperimentsSkeleton />}>
          <ExperimentsTableBody
            promise={experimentsPromise}
            authorFilter={authorFilter}
            statusFilter={statusFilter}
            searchQuery={searchQuery}
            currentExperimentsPage={currentExperimentsPage}
            onPreviousExperimentsPage={onPreviousExperimentsPage}
            onNextExperimentsPage={onNextExperimentsPage}
            onRefreshData={onRefreshData}
            onViewOrgExperiments={onViewOrgExperiments}
          />
        </Suspense>
      </CardContent>
    </Card>
  );
}

// Trial filter groups managed by the popover, keyed by their URL params.
// Mirrors the tasks sidebar's Trial group (label + per-group clear + h-8
// controls) so both surfaces read as the same filter system.
const TRIAL_FILTER_GROUPS: {
  label: string;
  keys: [string] | [string, string];
  placeholder?: string;
}[] = [
  { label: "Model", keys: ["model"], placeholder: "e.g. openai/gpt-5" },
  { label: "Trajectory length", keys: ["minSteps", "maxSteps"] },
  { label: "Trajectory time (s)", keys: ["minTime", "maxTime"] },
  { label: "Tool calls", keys: ["minTools", "maxTools"] },
  { label: "Tool names", keys: ["tool"], placeholder: "bash, read_file" },
];

function ExperimentTrialFilters() {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();

  const commit = (patch: Record<string, string>) => {
    const next = new URLSearchParams(searchParams.toString());
    for (const [key, value] of Object.entries(patch)) {
      if (value) next.set(key, value);
      else next.delete(key);
    }
    next.delete("page");
    const queryString = next.toString();
    router.push(queryString ? `${pathname}?${queryString}` : pathname);
  };

  const mode = searchParams.get("metricMatch") === "all" ? "all" : "any";
  const groupActive = (keys: readonly string[]) =>
    keys.some((key) => searchParams.get(key));
  const activeCount =
    TRIAL_FILTER_GROUPS.filter((group) => groupActive(group.keys)).length +
    (mode === "all" ? 1 : 0);
  const clearGroup = (keys: readonly string[]) =>
    commit(Object.fromEntries(keys.map((key) => [key, ""])));
  const help =
    "Any trial matches when one selected-model trial meets every metric constraint. All trials requires every selected-model trial to meet every constraint.";

  const field = (key: string, placeholder: string, numeric: boolean) => (
    <Input
      key={`${key}-${searchParams.get(key) ?? ""}`}
      defaultValue={searchParams.get(key) ?? ""}
      placeholder={placeholder}
      type={numeric ? "number" : "text"}
      min={numeric ? 0 : undefined}
      className="h-8 text-xs"
      onBlur={(event) => commit({ [key]: event.target.value.trim() })}
      onKeyDown={(event) => {
        if (event.key === "Enter") event.currentTarget.blur();
      }}
    />
  );

  return (
    <Popover>
      <PopoverTrigger asChild>
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="h-8 border-[#6f88b4]/20"
        >
          <Filter className="mr-1 h-3.5 w-3.5" />
          Filters
          {activeCount > 0 ? (
            <span className="text-muted-foreground ml-1 text-[11px]">
              ({activeCount})
            </span>
          ) : null}
        </Button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-72 p-3">
        <div className="mb-2 flex items-center justify-between">
          <span className="flex items-center gap-1.5 text-sm font-medium">
            <Filter className="h-3.5 w-3.5" />
            Trial filters
            {activeCount > 0 ? (
              <span className="text-muted-foreground text-[11px]">
                ({activeCount})
              </span>
            ) : null}
          </span>
          {activeCount > 0 ? (
            <button
              type="button"
              className="text-muted-foreground hover:text-foreground text-[11px]"
              onClick={() =>
                commit(
                  Object.fromEntries(
                    [
                      ...TRIAL_FILTER_GROUPS.flatMap((group) => group.keys),
                      "metricMatch",
                    ].map((key) => [key, ""])
                  )
                )
              }
            >
              Clear all
            </button>
          ) : null}
        </div>
        <div className="space-y-3">
          {TRIAL_FILTER_GROUPS.map((group) => (
            <div
              key={group.label}
              className="border-b border-[#6f88b4]/10 pb-3"
            >
              <div className="mb-1.5 flex items-center justify-between">
                <span className="text-xs font-medium">{group.label}</span>
                {groupActive(group.keys) ? (
                  <button
                    type="button"
                    aria-label={`Clear ${group.label} filter`}
                    className="text-muted-foreground hover:text-foreground"
                    onClick={() => clearGroup(group.keys)}
                  >
                    <X className="h-3 w-3" />
                  </button>
                ) : null}
              </div>
              {group.keys.length === 2 ? (
                <div className="flex items-center gap-1">
                  {field(group.keys[0], "min", true)}
                  <span className="text-muted-foreground text-xs">–</span>
                  {field(group.keys[1], "max", true)}
                </div>
              ) : (
                field(group.keys[0], group.placeholder ?? "", false)
              )}
            </div>
          ))}
          <div title={help}>
            <div className="mb-1.5 flex items-center justify-between">
              <span className="text-xs font-medium">Match metrics across</span>
              {mode === "all" ? (
                <button
                  type="button"
                  aria-label="Clear match mode filter"
                  className="text-muted-foreground hover:text-foreground"
                  onClick={() => commit({ metricMatch: "" })}
                >
                  <X className="h-3 w-3" />
                </button>
              ) : null}
            </div>
            <div className="grid grid-cols-2 rounded-md border p-0.5">
              {(["any", "all"] as const).map((value) => (
                <button
                  key={value}
                  type="button"
                  className={cn(
                    "rounded px-2 py-1.5 text-xs capitalize",
                    mode === value && "bg-primary text-primary-foreground"
                  )}
                  onClick={() =>
                    commit({ metricMatch: value === "all" ? "all" : "" })
                  }
                >
                  {value} trial{value === "all" ? "s" : ""}
                </button>
              ))}
            </div>
          </div>
        </div>
      </PopoverContent>
    </Popover>
  );
}

// =============================================================================
// Main Dashboard
// =============================================================================

type DashboardClientProps = {
  experimentsPromise: Promise<ExperimentsResult>;
  initialAuthor?: string;
  initialStatus?: string;
  initialQuery?: string;
  initialOffset?: number;
};

export function DashboardClient({
  experimentsPromise,
  initialAuthor = DASHBOARD_DEFAULT_EXPERIMENTS_AUTHOR,
  initialStatus = "all",
  initialQuery = "",
  initialOffset = 0,
}: DashboardClientProps) {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const [, startTransition] = useTransition();

  // Selection filters (Org/Mine, member, status) and the page come from the
  // URL and are resolved on the server, so the applied values are the SSR
  // props; changing one navigates (router.push) to re-render server-side.
  const authorFilter = initialAuthor;
  const statusFilter = initialStatus;
  const experimentsOffset = initialOffset;
  const currentExperimentsPage =
    Math.floor(experimentsOffset / EXPERIMENTS_PAGE_SIZE) + 1;

  // Local, editable search text; navigation (below) commits it to the URL.
  const [searchQuery, setSearchQuery] = useState(initialQuery);
  const members = useOrgMembers();

  // Keying the Suspense boundary on the committed (server) params makes it
  // re-suspend — and show the skeleton — on every content change.
  const paramsKey = `${authorFilter}|${statusFilter}|${initialQuery}|${experimentsOffset}`;

  // Build a dashboard URL for the given selection/page, preserving the current
  // search and any params this helper doesn't manage (e.g. trial metric
  // filters). Defaults are omitted so the clean view stays "/dashboard".
  const buildFilterHref = (overrides: {
    author?: string;
    status?: string;
    page?: number;
  }) => {
    const author = overrides.author ?? authorFilter;
    const status = overrides.status ?? statusFilter;
    const page = overrides.page ?? 1;
    const query = searchQuery.trim();
    const params = new URLSearchParams(searchParams.toString());
    params.delete("author");
    params.delete("status");
    params.delete("q");
    params.delete("page");
    if (author !== DASHBOARD_DEFAULT_EXPERIMENTS_AUTHOR) {
      params.set("author", author);
    }
    if (status !== "all") {
      params.set("status", status);
    }
    if (query) {
      params.set("q", query);
    }
    if (page > 1) {
      params.set("page", String(page));
    }
    const queryString = params.toString();
    return queryString ? `${pathname}?${queryString}` : pathname;
  };

  // Selection / pagination / search changes navigate so the server re-renders
  // and the keyed Suspense streams a fresh skeleton + results.
  const navigateToFilters = (overrides: {
    author?: string;
    status?: string;
    page?: number;
  }) => {
    startTransition(() =>
      router.push(buildFilterHref(overrides), { scroll: false })
    );
  };

  const handleAuthorFilterChange = (author: string) =>
    navigateToFilters({ author });
  const handleStatusFilterChange = (status: string) =>
    navigateToFilters({ status });
  const handlePreviousExperimentsPage = () => {
    if (currentExperimentsPage <= 1) return;
    navigateToFilters({ page: currentExperimentsPage - 1 });
  };
  const handleNextExperimentsPage = () => {
    navigateToFilters({ page: currentExperimentsPage + 1 });
  };
  const handleRefreshData = () => {
    router.refresh();
  };

  // Debounce the search box, then commit it to the URL (page reset to 1).
  // Skips the first render so a deep-linked search isn't immediately re-pushed.
  const isInitialSearchMount = useRef(true);
  useEffect(() => {
    if (isInitialSearchMount.current) {
      isInitialSearchMount.current = false;
      return;
    }
    const handle = window.setTimeout(() => {
      if (searchQuery.trim() === initialQuery.trim()) return;
      navigateToFilters({ page: 1 });
    }, 400);
    return () => window.clearTimeout(handle);
    // navigateToFilters/initialQuery are intentionally excluded: they are
    // rebuilt every render and we only want to react to search edits.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchQuery]);

  return (
    <div className="space-y-4">
      <QuotaUsageCard />
      <UsageSummaryCard />
      <CostLeaderboardStrip />
      <RecentTasksCard
        experimentsPromise={experimentsPromise}
        paramsKey={paramsKey}
        searchQuery={searchQuery}
        onSearchQueryChange={setSearchQuery}
        statusFilter={statusFilter}
        onStatusFilterChange={handleStatusFilterChange}
        authorFilter={authorFilter}
        onAuthorFilterChange={handleAuthorFilterChange}
        members={members}
        currentExperimentsPage={currentExperimentsPage}
        onPreviousExperimentsPage={handlePreviousExperimentsPage}
        onNextExperimentsPage={handleNextExperimentsPage}
        onRefreshData={handleRefreshData}
        onViewOrgExperiments={() => handleAuthorFilterChange("all")}
      />
    </div>
  );
}
