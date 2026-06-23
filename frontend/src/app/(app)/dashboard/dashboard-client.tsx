"use client";

import { useDeferredValue, useEffect, useState } from "react";
import useSWR, { useSWRConfig } from "swr";
import Link from "next/link";
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
import type {
  DashboardExperiment,
  DashboardExperimentAuthor,
  DashboardResponse,
  OrgUser,
} from "@/lib/types";
import { fetcher } from "@/lib/api";
import { parseTaskSearch } from "@/lib/tag-query";
import {
  SearchSyntaxHelp,
  SearchSyntaxMultiRow,
  SearchSyntaxRow,
} from "@/components/search-syntax-help";
import { TagChip } from "@/components/tag-chip";
import {
  cn,
  encodeExperimentRouteParam,
  formatShortDateTime,
  prBadge,
} from "@/lib/utils";
import {
  buildDashboardApiPath,
  DASHBOARD_DEFAULT_EXPERIMENTS_AUTHOR,
  DASHBOARD_DEFAULT_EXPERIMENTS_LIMIT,
  isDefaultDashboardExperimentsView,
} from "@/lib/dashboard-request";
import { badgeVariants } from "@/components/ui/badge";
import {
  ArrowRight,
  Check,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  Clock,
  Copy,
  ExternalLink,
  GitPullRequest,
  Trash2,
  Globe,
  Key,
  Terminal,
  Users,
} from "lucide-react";

// =============================================================================
// Dashboard Hook - Single API call for all data
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

function useDashboardExperiments(
  experimentsLimit: number,
  experimentsOffset: number,
  experimentsQuery: string,
  experimentsStatus: string,
  experimentsAuthor: string,
  fallbackData?: DashboardResponse | null,
) {
  // tag:/-tag:/OR/NOT tokens filter server-side; remaining text keeps the
  // name/id/author search semantics (same grammar as the /tasks page).
  const parsedQuery = parseTaskSearch(experimentsQuery);
  const swrKey = buildDashboardApiPath({
    experiments_limit: experimentsLimit,
    experiments_offset: experimentsOffset,
    experiments_query: parsedQuery.text,
    experiments_tags: parsedQuery.all.join(","),
    experiments_tags_any: parsedQuery.any.join(","),
    experiments_tags_none: parsedQuery.none.join(","),
    experiments_author_query: parsedQuery.authors.join(","),
    experiments_status: experimentsStatus,
    experiments_author: experimentsAuthor,
    include_tasks: false,
    include_usage: false,
  });
  const hasFallbackData = fallbackData != null;

  const { data, error, isLoading, isValidating } = useSWR<DashboardResponse>(
    swrKey,
    fetcher,
    {
      refreshInterval: 45000,
      revalidateOnFocus: false,
      revalidateOnMount: !hasFallbackData,
      revalidateIfStale: !hasFallbackData,
      keepPreviousData: true,
      fallbackData: hasFallbackData ? (fallbackData ?? undefined) : undefined,
    },
  );

  return {
    experiments: data?.experiments ?? [],
    hasMoreExperiments: data?.experiments_has_more ?? false,
    swrKey,
    error,
    isLoading,
    isValidating,
  };
}

function formatTaskAuthor(author: DashboardExperimentAuthor | null): string {
  if (!author) return "—";
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
    <div className="flex items-center gap-2 rounded-md border border-border/80 bg-muted/35 px-3 py-2">
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

function FirstRunCard() {
  return (
    <Card className="border-[#85b85c]/25 bg-card/95 shadow-xs">
      <CardContent className="flex flex-col gap-4 p-5 sm:flex-row sm:items-center sm:justify-between">
        <div className="space-y-2">
          <p className="text-sm font-medium">Set up your first Oddish run</p>
          <div className="flex flex-wrap gap-2 text-xs text-muted-foreground">
            <span className="rounded-full border border-[#85b85c]/25 bg-background/70 px-2 py-1">
              1. Install CLI
            </span>
            <span className="rounded-full border border-[#6f88b4]/25 bg-background/70 px-2 py-1">
              2. Export API key
            </span>
            <span className="rounded-full border border-[#85b85c]/25 bg-background/70 px-2 py-1">
              3. Submit job
            </span>
          </div>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button asChild size="sm">
            <Link href="/settings?tab=api-keys">
              API keys
              <ArrowRight className="h-3.5 w-3.5" />
            </Link>
          </Button>
          <Button asChild variant="outline" size="sm">
            <a
              href="https://github.com/abundant-ai/oddish#quick-start"
              target="_blank"
              rel="noreferrer"
            >
              Quick start
            </a>
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

function EmptyExperimentsState() {
  return (
    <div className="rounded-lg border border-dashed border-[#6f88b4]/30 bg-card/60 p-6">
      <div className="flex flex-col items-center text-center">
        <Clock className="mb-3 h-11 w-11 text-muted-foreground/70" />
        <p className="text-base font-medium">No experiments yet</p>
      </div>

      <div className="mt-5 grid gap-3 lg:grid-cols-3">
        <div className="rounded-lg border border-[#85b85c]/20 bg-background/80 p-4">
          <div className="mb-3 flex items-center gap-2 text-sm font-medium">
            <Terminal className="h-4 w-4 text-[#5c8e43]" />
            Install the CLI
          </div>
          <CommandSnippet command="uv pip install oddish" />
        </div>

        <div className="rounded-lg border border-[#6f88b4]/20 bg-background/80 p-4">
          <div className="mb-3 flex items-center gap-2 text-sm font-medium">
            <Key className="h-4 w-4 text-[#6f88b4]" />
            Add an API key
          </div>
          <CommandSnippet command={'export ODDISH_API_KEY="ok_..."'} />
        </div>

        <div className="rounded-lg border border-[#85b85c]/20 bg-background/80 p-4">
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
    <div className="rounded-lg border border-dashed border-[#6f88b4]/30 bg-card/60 p-6">
      <div className="flex flex-col items-center text-center">
        <Users className="mb-3 h-11 w-11 text-muted-foreground/70" />
        <p className="text-base font-medium">No experiments of yours yet</p>
        <p className="mt-1 max-w-md text-sm text-muted-foreground">
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
// Recent Tasks Card
// =============================================================================

function RecentTasksCard({
  experiments,
  searchQuery,
  onSearchQueryChange,
  statusFilter,
  onStatusFilterChange,
  authorFilter,
  onAuthorFilterChange,
  members,
  error,
  isLoading,
  hasMoreExperiments,
  onPreviousExperimentsPage,
  onNextExperimentsPage,
  isPageTransitioning,
  onRefreshData,
  currentExperimentsPage,
  onViewOrgExperiments,
}: {
  experiments: DashboardExperiment[];
  searchQuery: string;
  onSearchQueryChange: (value: string) => void;
  statusFilter: string;
  onStatusFilterChange: (value: string) => void;
  authorFilter: string;
  onAuthorFilterChange: (value: string) => void;
  members: OrgUser[];
  error: Error | undefined;
  isLoading: boolean;
  hasMoreExperiments: boolean;
  onPreviousExperimentsPage: () => void;
  onNextExperimentsPage: () => void;
  isPageTransitioning: boolean;
  onRefreshData: () => Promise<void>;
  currentExperimentsPage: number;
  onViewOrgExperiments: () => void;
}) {
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
  const statusFilterLabel =
    STATUS_FILTER_OPTIONS.find((option) => option.value === statusFilter)
      ?.label ?? "Filter status";
  const selectedMember = isMemberSelected
    ? members.find((member) => member.id === authorFilter)
    : undefined;
  const memberFilterLabel = selectedMember
    ? memberDisplayName(selectedMember)
    : "Members";

  const handleDeleteExperiment = async () => {
    if (!deleteTarget || isDeleting) return;
    setIsDeleting(true);
    setDeleteError(null);

    try {
      const res = await fetch(
        `/api/experiments/${encodeExperimentRouteParam(deleteTarget.id)}`,
        { method: "DELETE" },
      );

      if (!res.ok) {
        const errorData = await res.json().catch(() => ({}));
        throw new Error(
          errorData.detail || errorData.error || "Failed to delete experiment",
        );
      }

      await onRefreshData();
      setDeleteTarget(null);
    } catch (error) {
      setDeleteError(
        error instanceof Error ? error.message : "Failed to delete experiment",
      );
    } finally {
      setIsDeleting(false);
    }
  };

  return (
    <Card className="col-span-5 border-[#6f88b4]/20 shadow-xs">
      <CardHeader className="flex flex-col gap-3 pb-3 sm:flex-row sm:items-center sm:justify-between">
        <div className="space-y-1">
          <CardTitle className="text-base">Recent Experiments</CardTitle>
          <div className="text-[11px] text-muted-foreground">
            Showing {experiments.length}
            {" • "}
            Page {currentExperimentsPage}
            {isPageTransitioning ? " • Loading..." : ""}
          </div>
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
                    <DropdownMenuRadioItem key={member.id} value={member.id}>
                      <span className="flex min-w-0 flex-col">
                        <span className="truncate">
                          {memberDisplayName(member)}
                        </span>
                        {member.github_username && (
                          <span className="truncate text-[10px] text-muted-foreground">
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
                  Matches experiment name, author, or tags. Add a
                  prefix below to specify filters.
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
                Filters stack (AND) and are case-insensitive, e.g. {" "}
                <code className="rounded bg-muted px-1 font-mono">
                  github:alice tag:smoke
                </code>
              </p>
            </SearchSyntaxHelp>
          </div>
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
        </div>
      </CardHeader>
      <CardContent>
        {error && experiments.length === 0 ? (
          <Alert variant="destructive">
            <AlertTitle>Failed to load experiments</AlertTitle>
            <AlertDescription>
              Check the API connection and try again.
            </AlertDescription>
          </Alert>
        ) : isLoading ? (
          <p className="text-muted-foreground">Loading...</p>
        ) : (
          <>
            {error ? (
              <p className="mb-2 text-xs text-destructive">
                Refresh failed — showing the last loaded results.
              </p>
            ) : null}
            {!isLoading &&
            experiments.length === 0 &&
            !hasMoreExperiments &&
            !hasFilters ? (
              authorFilter === "me" ? (
                <MineEmptyExperimentsState
                  onViewOrgExperiments={onViewOrgExperiments}
                />
              ) : (
                <EmptyExperimentsState />
              )
            ) : experiments.length === 0 ? (
              <div className="py-8 text-center text-muted-foreground">
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
                      const retryingTrials =
                        Number(experiment.retrying_trials) || 0;

                      return (
                        <TableRow key={experiment.id}>
                          <TableCell>
                            <div className="flex items-center gap-1.5">
                              <Link
                                href={`/experiments/${encodeExperimentRouteParam(
                                  experiment.id,
                                )}`}
                                className="text-[#5d77a5] transition-colors hover:text-[#526a95] dark:text-[#a8b8d2] dark:hover:text-[#c0cde1]"
                              >
                                {experiment.name}
                              </Link>
                              {experiment.is_public && (
                                <Globe
                                  className="h-3.5 w-3.5 text-muted-foreground"
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
                          <TableCell className="whitespace-nowrap text-xs text-muted-foreground">
                            <span className="text-foreground/80">
                              {formatTaskAuthor(
                                experiment.author ?? experiment.last_author,
                              )}
                            </span>
                          </TableCell>
                          <TableCell className="whitespace-nowrap text-xs text-muted-foreground">
                            <span className="text-foreground/80">
                              {formatTaskAuthor(
                                experiment.last_runner ??
                                  experiment.last_author,
                              )}
                            </span>
                          </TableCell>
                          <TableCell className="whitespace-nowrap text-xs">
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
                                  "max-w-[200px] gap-1.5 font-mono text-[11px] transition-colors hover:bg-accent",
                                )}
                              >
                                <GitPullRequest
                                  className="h-3 w-3 shrink-0"
                                  aria-hidden
                                />
                                {(() => {
                                  const { label, number } = prBadge(
                                    experiment.last_pr_url,
                                    experiment.last_pr_number,
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
                          <TableCell className="whitespace-nowrap font-mono text-xs">
                            {experiment.completed_trials}/
                            {experiment.total_trials}
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
                          <TableCell className="whitespace-nowrap text-right text-xs text-muted-foreground">
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
                              className="h-8 w-8 text-destructive hover:text-destructive"
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
                    disabled={
                      currentExperimentsPage <= 1 || isPageTransitioning
                    }
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
                    disabled={!hasMoreExperiments || isPageTransitioning}
                  >
                    Next page
                    <ChevronRight className="ml-1 h-3.5 w-3.5" />
                  </Button>
                </div>
              </div>
            )}
          </>
        )}
      </CardContent>
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
              <span className="font-medium text-foreground">
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
    </Card>
  );
}

// =============================================================================
// Main Dashboard
// =============================================================================

type DashboardClientProps = {
  initialDashboardData?: DashboardResponse | null;
};

export function DashboardClient({
  initialDashboardData = null,
}: DashboardClientProps) {
  const { mutate } = useSWRConfig();
  const [experimentsOffset, setExperimentsOffset] = useState(0);
  const [searchQuery, setSearchQuery] = useState("");
  const deferredSearchQuery = useDeferredValue(searchQuery);
  const [statusFilter, setStatusFilter] = useState("all");
  const [authorFilter, setAuthorFilter] = useState(
    DASHBOARD_DEFAULT_EXPERIMENTS_AUTHOR,
  );
  const members = useOrgMembers();
  const experimentsFallbackData =
    isDefaultDashboardExperimentsView(
      experimentsOffset,
      deferredSearchQuery,
      statusFilter,
      authorFilter,
    ) && initialDashboardData?.experiments != null
      ? initialDashboardData
      : null;
  const {
    experiments,
    hasMoreExperiments,
    swrKey: experimentsSwrKey,
    error: experimentsError,
    isLoading: isExperimentsLoading,
    isValidating: isExperimentsValidating,
  } = useDashboardExperiments(
    EXPERIMENTS_PAGE_SIZE,
    experimentsOffset,
    deferredSearchQuery,
    statusFilter,
    authorFilter,
    experimentsFallbackData,
  );
  const currentExperimentsPage =
    Math.floor(experimentsOffset / EXPERIMENTS_PAGE_SIZE) + 1;
  const isDefaultOrgExperimentsEmpty =
    experiments.length === 0 &&
    !hasMoreExperiments &&
    !isExperimentsLoading &&
    !experimentsError &&
    currentExperimentsPage === 1 &&
    deferredSearchQuery.trim().length === 0 &&
    statusFilter === "all" &&
    authorFilter === "all";

  useEffect(() => {
    setExperimentsOffset(0);
  }, [deferredSearchQuery, statusFilter, authorFilter]);

  const handlePreviousExperimentsPage = () => {
    setExperimentsOffset((prev) => Math.max(0, prev - EXPERIMENTS_PAGE_SIZE));
  };

  const handleNextExperimentsPage = () => {
    if (!hasMoreExperiments) return;
    setExperimentsOffset((prev) => prev + EXPERIMENTS_PAGE_SIZE);
  };

  const handleRefreshCurrentPage = async () => {
    await mutate(experimentsSwrKey);
  };

  return (
    <div className="space-y-4">
      {isDefaultOrgExperimentsEmpty && <FirstRunCard />}
      <RecentTasksCard
        experiments={experiments}
        searchQuery={searchQuery}
        onSearchQueryChange={setSearchQuery}
        statusFilter={statusFilter}
        onStatusFilterChange={setStatusFilter}
        authorFilter={authorFilter}
        onAuthorFilterChange={setAuthorFilter}
        members={members}
        error={experimentsError}
        isLoading={isExperimentsLoading}
        hasMoreExperiments={hasMoreExperiments}
        onPreviousExperimentsPage={handlePreviousExperimentsPage}
        onNextExperimentsPage={handleNextExperimentsPage}
        isPageTransitioning={isExperimentsLoading || isExperimentsValidating}
        onRefreshData={handleRefreshCurrentPage}
        currentExperimentsPage={currentExperimentsPage}
        onViewOrgExperiments={() => setAuthorFilter("all")}
      />
    </div>
  );
}
