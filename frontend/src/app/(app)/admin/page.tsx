"use client";

import { Suspense, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
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
import type { QueueSlotsResponse, QueueSlotSummary } from "@/lib/types";
import { fetcher } from "@/lib/api";
import { QueueKeyIcon } from "@/components/queue-key-icon";
import { TagAdminPolicyForm } from "@/components/tag-admin-policy-form";
import { QuotaAdminForm } from "@/components/quota-admin-form";
import { WorkerJobsCard } from "@/components/worker-jobs-card";
import { UsagePanel } from "@/components/usage-panel";
import { QueueHealthOverviewCard } from "@/components/queue-health-overview-card";
import { CostBreakdownCard } from "@/components/cost-breakdown-card";
import { CostExclusionsCard } from "@/components/cost-exclusions-card";
import { SlackAlertSettingsForm } from "@/components/slack-alert-settings-form";
import { RefreshCw, Server, Clock, AlertCircle } from "lucide-react";

// =============================================================================
// Queue Slots Card
// =============================================================================

function QueueSlotsCard() {
  const { data, error, isLoading, mutate } = useSWR<QueueSlotsResponse>(
    `/api/admin/slots`,
    fetcher,
    {
      refreshInterval: 10000,
    }
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
  const queueNeedle = queueFilter.toLowerCase().trim();
  const filteredProviders =
    data?.queue_keys.filter(
      (queueSummary) =>
        !queueNeedle ||
        queueSummary.queue_key.toLowerCase().includes(queueNeedle)
    ) ?? [];

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
          <p className="text-muted-foreground text-xs">
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
          <div className="text-muted-foreground py-8 text-center">
            <Server className="mx-auto mb-3 h-12 w-12 opacity-50" />
            <p>No queue slots configured</p>
          </div>
        ) : (
          <div className="space-y-3">
            <Input
              value={queueFilter}
              onChange={(event) => setQueueFilter(event.target.value)}
              placeholder="Filter queue keys or job kinds..."
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
                          {/* Fixed-width fill bar instead of one-dot-per-slot:
                              high-concurrency keys (up to 128 slots) used to
                              paint a dot row that ran off the screen. */}
                          <div className="bg-muted-foreground/20 h-2 w-20 overflow-hidden rounded-full">
                            <div
                              className="h-full bg-blue-500"
                              style={{
                                width: `${
                                  queueSummary.total_slots > 0
                                    ? Math.min(
                                        100,
                                        (queueSummary.active_slots /
                                          queueSummary.total_slots) *
                                          100
                                      )
                                    : 0
                                }%`,
                              }}
                            />
                          </div>
                          <Badge
                            variant={
                              queueSummary.active_slots > 0
                                ? "default"
                                : "outline"
                            }
                            className="font-mono text-xs"
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
              <p className="text-muted-foreground text-xs">
                No queue keys match the current filter.
              </p>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

const ADMIN_TABS = [
  "overview",
  "usage",
  "costs",
  "worker-jobs",
  "concurrency",
  "tags",
  "quotas",
] as const;

// Platform-wide config: hidden unless the caller is in the operator org.
const OPERATOR_ONLY_TABS: ReadonlySet<(typeof ADMIN_TABS)[number]> = new Set([
  "concurrency",
]);

function AdminPageContent() {
  // The active tab lives in the URL (?tab=usage) so tabs are deep-linkable
  // (e.g. from the dashboard usage card) and browser back/forward moves
  // between them.
  const router = useRouter();
  const searchParams = useSearchParams();
  const {
    data: operatorAccess,
    error: operatorAccessError,
    mutate: retryOperatorAccess,
  } = useSWR<{ allowed: boolean }>("/api/admin/operator-access", fetcher);
  const operatorAccessResolved = operatorAccess !== undefined;
  const canManagePlatform = operatorAccess?.allowed === true;
  const allowedTabs = canManagePlatform
    ? ADMIN_TABS
    : ADMIN_TABS.filter((tab) => !OPERATOR_ONLY_TABS.has(tab));
  const requestedTab = searchParams.get("tab") ?? "";
  const activeTab = (allowedTabs as readonly string[]).includes(requestedTab)
    ? requestedTab
    : "overview";
  const handleTabChange = (value: string) => {
    router.push(value === "overview" ? "/admin" : `/admin?tab=${value}`, {
      scroll: false,
    });
  };

  // An unknown ?tab= (typo, removed tab) falls back to overview on screen;
  // rewrite the URL to match so bookmarks and back/forward stay consistent.
  useEffect(() => {
    if (operatorAccessResolved && requestedTab && requestedTab !== activeTab) {
      router.replace("/admin", { scroll: false });
    }
  }, [operatorAccessResolved, requestedTab, activeTab, router]);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">Admin Dashboard</h1>
        <p className="text-muted-foreground text-sm">
          Organization usage, spend, queues, tags, and quotas
        </p>
      </div>

      {operatorAccessError && operatorAccess === undefined && (
        <Alert variant="destructive">
          <AlertCircle className="h-4 w-4" />
          <AlertTitle>Operator controls unavailable</AlertTitle>
          <AlertDescription className="flex items-center justify-between gap-4">
            The capability check failed. Global controls remain hidden until it
            succeeds.
            <Button
              variant="outline"
              size="sm"
              onClick={() => retryOperatorAccess()}
            >
              Retry
            </Button>
          </AlertDescription>
        </Alert>
      )}

      <Tabs
        value={activeTab}
        onValueChange={handleTabChange}
        className="space-y-4"
      >
        <TabsList>
          <TabsTrigger value="overview">Overview</TabsTrigger>
          <TabsTrigger value="usage">Usage</TabsTrigger>
          <TabsTrigger value="costs">Costs</TabsTrigger>
          <TabsTrigger value="worker-jobs">Worker Jobs</TabsTrigger>
          {canManagePlatform && (
            <TabsTrigger value="concurrency">Concurrency</TabsTrigger>
          )}
          <TabsTrigger value="tags">Tag Policy</TabsTrigger>
          <TabsTrigger value="quotas">Quotas</TabsTrigger>
        </TabsList>

        <TabsContent value="overview" className="space-y-4">
          <QueueHealthOverviewCard canManageConcurrency={canManagePlatform} />
        </TabsContent>

        <TabsContent value="usage" className="space-y-4">
          <UsagePanel />
        </TabsContent>

        <TabsContent value="costs" className="space-y-4">
          <CostBreakdownCard />
          {canManagePlatform && (
            <>
              <Card>
                <CardHeader>
                  <CardTitle className="text-base">
                    Channel spend escalation
                  </CardTitle>
                </CardHeader>
                <CardContent>
                  <SlackAlertSettingsForm />
                </CardContent>
              </Card>
              <CostExclusionsCard />
            </>
          )}
        </TabsContent>

        <TabsContent value="worker-jobs" className="space-y-4">
          <WorkerJobsCard />
        </TabsContent>

        {canManagePlatform && (
          <TabsContent value="concurrency" className="space-y-4">
            <QueueSlotsCard />
          </TabsContent>
        )}

        <TabsContent value="tags" className="space-y-4">
          <Card>
            <CardHeader>
              <CardTitle>Tag policy</CardTitle>
            </CardHeader>
            <CardContent>
              <TagAdminPolicyForm />
            </CardContent>
          </Card>
        </TabsContent>

        <TabsContent value="quotas" className="space-y-4">
          <Card>
            <CardHeader>
              <CardTitle>User quotas</CardTitle>
            </CardHeader>
            <CardContent>
              <QuotaAdminForm />
            </CardContent>
          </Card>
        </TabsContent>
      </Tabs>
    </div>
  );
}

export default function AdminPage() {
  // useSearchParams requires a Suspense boundary for static prerendering.
  return (
    <Suspense>
      <AdminPageContent />
    </Suspense>
  );
}
