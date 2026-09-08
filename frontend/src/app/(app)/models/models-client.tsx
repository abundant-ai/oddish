"use client";

import { Fragment, useState } from "react";
import useSWR from "swr";
import {
  AlertCircle,
  ArrowDown,
  ArrowUp,
  ArrowUpDown,
  Search,
  CheckCircle2,
  ChevronDown,
  Play,
  RefreshCw,
  XCircle,
} from "lucide-react";

import { CodeBlock } from "@/components/code-block";
import { QueueKeyIcon } from "@/components/queue-key-icon";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Input } from "@/components/ui/input";
import {
  endpointKey,
  modelCatalogRows,
  ROUTE_LABELS,
  type ModelCheckState,
  type ModelSort,
  type ModelSortField,
  type ModelStatus,
} from "@/lib/model-catalog";
import { fetcher } from "@/lib/api";
import type {
  ModelEndpointCatalogResponse,
  ModelEndpointCheckResponse,
  ModelEndpointSummary,
} from "@/lib/types";

const MODEL_CHECK_BATCH_SIZE = 3;

export function ModelsClient() {
  const { data, error, isLoading, mutate } =
    useSWR<ModelEndpointCatalogResponse>("/api/models", fetcher);
  const [checks, setChecks] = useState<Record<string, ModelCheckState>>({});
  const [expandedModel, setExpandedModel] = useState<string | null>(null);

  const [query, setQuery] = useState("");
  const [provider, setProvider] = useState("all");
  const [status, setStatus] = useState<ModelStatus | "all">("all");
  const [sort, setSort] = useState<ModelSort>({
    field: "name",
    direction: "asc",
  });
  const [isBatchRunning, setIsBatchRunning] = useState(false);
  const rows = modelCatalogRows(
    data?.models ?? [],
    checks,
    query,
    provider,
    status,
    sort
  );
  const providers = [...new Set(data?.models.map(({ route }) => route))].sort(
    (a, b) => (ROUTE_LABELS[a] ?? a).localeCompare(ROUTE_LABELS[b] ?? b)
  );
  const providerCount = new Set(rows.map(({ endpoint }) => endpoint.route))
    .size;
  const hasFilters = query.length > 0 || provider !== "all" || status !== "all";

  function clearFilters() {
    setQuery("");
    setProvider("all");
    setStatus("all");
  }

  function sortBy(field: ModelSortField) {
    setSort({
      field,
      direction:
        sort.field === field && sort.direction === "asc"
          ? "desc"
          : field === "testedAt" && sort.field !== field
            ? "desc"
            : "asc",
    });
  }

  const hasRunningCheck =
    isBatchRunning ||
    Object.values(checks).some((check) => check.status === "running");
  const passing = rows.filter(({ status }) => status === "Passed").length;
  const failing = rows.filter(({ status }) => status === "Failed").length;
  const testableModels = rows
    .map(({ endpoint }) => endpoint)
    .filter(({ testable }) => testable);

  async function testModel(
    endpoint: ModelEndpointSummary,
    expand: boolean
  ): Promise<boolean> {
    const key = endpointKey(endpoint);
    if (expand) setExpandedModel(key);
    setChecks((current) => ({
      ...current,
      [key]: { status: "running" },
    }));

    try {
      const result = await fetcher<ModelEndpointCheckResponse>(
        "/api/models/check",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            model: endpoint.model,
            route: endpoint.route,
          }),
        }
      );
      setChecks((current) => ({
        ...current,
        [key]: { status: "complete", result, testedAt: Date.now() },
      }));
      return result.ok;
    } catch (checkError) {
      setChecks((current) => ({
        ...current,
        [key]: {
          status: "error",
          message:
            checkError instanceof Error ? checkError.message : "Request failed",
          testedAt: Date.now(),
        },
      }));
      return false;
    }
  }

  async function testMatchingModels() {
    if (!testableModels.length || hasRunningCheck) return;
    setIsBatchRunning(true);
    try {
      const outcomes: { key: string; ok: boolean }[] = [];
      for (
        let index = 0;
        index < testableModels.length;
        index += MODEL_CHECK_BATCH_SIZE
      ) {
        const batch = testableModels.slice(
          index,
          index + MODEL_CHECK_BATCH_SIZE
        );
        outcomes.push(
          ...(await Promise.all(
            batch.map(async (endpoint) => ({
              key: endpointKey(endpoint),
              ok: await testModel(endpoint, false),
            }))
          ))
        );
      }
      setExpandedModel(
        outcomes.find((outcome) => !outcome.ok)?.key ?? outcomes[0]?.key ?? null
      );
    } finally {
      setIsBatchRunning(false);
    }
  }

  return (
    <div className="space-y-5">
      <div className="flex flex-col justify-between gap-3 sm:flex-row sm:items-end">
        <div>
          <h1 className="text-2xl font-bold">Models</h1>
          <p className="text-muted-foreground mt-1 text-sm">
            Find a model and test whether it returns a text response using
            Oddish credentials.
          </p>
        </div>
      </div>

      {error ? (
        <Alert variant="destructive">
          <AlertCircle className="h-4 w-4" />
          <AlertTitle>Failed to load models</AlertTitle>
          <AlertDescription className="flex items-center justify-between gap-3">
            {error instanceof Error ? error.message : "Request failed"}
            <Button variant="outline" size="sm" onClick={() => void mutate()}>
              Retry
            </Button>
          </AlertDescription>
        </Alert>
      ) : data && !data.allowed ? (
        <Alert>
          <AlertCircle className="h-4 w-4" />
          <AlertTitle>Model checks unavailable</AlertTitle>
          <AlertDescription>
            Direct provider checks are available only in the operator workspace.
          </AlertDescription>
        </Alert>
      ) : (
        <Card>
          <CardHeader className="border-b">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <CardTitle className="text-base">Available models</CardTitle>
              <span role="status" className="text-muted-foreground text-sm">
                {rows.length} of {data?.models.length ?? 0} models ·{" "}
                {providerCount} {providerCount === 1 ? "provider" : "providers"}
                {(passing > 0 || failing > 0) &&
                  ` · ${passing} passing · ${failing} failing`}
              </span>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <div className="relative min-w-48 flex-1">
                <Search
                  aria-hidden="true"
                  className="text-muted-foreground pointer-events-none absolute top-2.5 left-3 h-4 w-4"
                />
                <Input
                  type="search"
                  aria-label="Search models"
                  placeholder="Search models or providers…"
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  className="pl-9"
                />
              </div>
              <select
                aria-label="Filter by provider"
                value={provider}
                onChange={(event) => setProvider(event.target.value)}
                className="bg-background h-9 max-w-full rounded-md border px-3 text-sm"
              >
                <option value="all">All providers</option>
                {providers.map((route) => (
                  <option key={route} value={route}>
                    {ROUTE_LABELS[route] ?? route}
                  </option>
                ))}
              </select>
              <select
                aria-label="Filter by status"
                value={status}
                onChange={(event) =>
                  setStatus(event.target.value as ModelStatus | "all")
                }
                className="bg-background h-9 rounded-md border px-3 text-sm"
              >
                <option value="all">All statuses</option>
                {["Not tested", "Testing", "Passed", "Failed", "CLI only"].map(
                  (value) => (
                    <option key={value} value={value}>
                      {value}
                    </option>
                  )
                )}
              </select>
              <select
                aria-label="Sort models by"
                value={sort.field}
                onChange={(event) => {
                  const field = event.target.value as ModelSortField;
                  setSort({
                    field,
                    direction: field === "testedAt" ? "desc" : "asc",
                  });
                }}
                className="bg-background h-9 rounded-md border px-3 text-sm"
              >
                <option value="name">Sort: Model name</option>
                <option value="provider">Sort: Provider</option>
                <option value="status">Sort: Status</option>
                <option value="latency">Sort: Latency</option>
                <option value="testedAt">Sort: Last tested</option>
              </select>
              <Button
                variant="outline"
                size="icon"
                aria-label={`Sort ${sort.direction === "asc" ? "descending" : "ascending"}`}
                onClick={() =>
                  setSort({
                    ...sort,
                    direction: sort.direction === "asc" ? "desc" : "asc",
                  })
                }
              >
                {sort.direction === "asc" ? (
                  <ArrowUp className="h-4 w-4" />
                ) : (
                  <ArrowDown className="h-4 w-4" />
                )}
              </Button>
              {hasFilters && (
                <Button variant="ghost" size="sm" onClick={clearFilters}>
                  Clear filters
                </Button>
              )}
              <Button
                variant="outline"
                disabled={
                  !data?.allowed || !testableModels.length || hasRunningCheck
                }
                onClick={() => void testMatchingModels()}
              >
                {hasRunningCheck ? (
                  <RefreshCw className="h-4 w-4 animate-spin" />
                ) : (
                  <Play className="h-4 w-4" />
                )}
                {isBatchRunning
                  ? "Testing models…"
                  : `Test ${testableModels.length} matching ${testableModels.length === 1 ? "model" : "models"}`}
              </Button>
            </div>
          </CardHeader>
          <CardContent className="p-0">
            {isLoading ? (
              <div className="text-muted-foreground px-4 py-10 text-center text-sm">
                Loading models...
              </div>
            ) : !data?.models.length ? (
              <div className="text-muted-foreground px-4 py-10 text-center text-sm">
                No model queue keys are configured.
              </div>
            ) : !rows.length ? (
              <div className="text-muted-foreground space-y-2 px-4 py-10 text-center text-sm">
                <p>No models match your search and filters.</p>
                <Button variant="outline" size="sm" onClick={clearFilters}>
                  Clear filters
                </Button>
              </div>
            ) : (
              <Table className="table-fixed sm:table-auto">
                <TableHeader>
                  <TableRow>
                    {(
                      [
                        {
                          field: "name",
                          label: "Model",
                          className: "w-[44%] sm:w-auto",
                        },
                        {
                          field: "provider",
                          label: "Provider",
                          className: "hidden lg:table-cell",
                        },
                        {
                          field: "status",
                          label: "Status",
                          className: "w-[28%] sm:w-auto",
                        },
                        {
                          field: "latency",
                          label: "Latency",
                          className: "hidden md:table-cell",
                        },
                        {
                          field: "testedAt",
                          label: "Last tested",
                          className: "hidden md:table-cell",
                        },
                      ] as const
                    ).map(({ field, label, className }) => (
                      <TableHead
                        key={field}
                        className={className}
                        aria-sort={
                          sort.field === field
                            ? sort.direction === "asc"
                              ? "ascending"
                              : "descending"
                            : "none"
                        }
                      >
                        <button
                          type="button"
                          className="hover:text-foreground focus-visible:ring-ring inline-flex items-center gap-1 rounded py-2 focus-visible:ring-2 focus-visible:outline-none"
                          onClick={() => sortBy(field)}
                        >
                          {label}
                          {sort.field !== field ? (
                            <ArrowUpDown
                              aria-hidden="true"
                              className="h-3 w-3"
                            />
                          ) : sort.direction === "asc" ? (
                            <ArrowUp aria-hidden="true" className="h-3 w-3" />
                          ) : (
                            <ArrowDown aria-hidden="true" className="h-3 w-3" />
                          )}
                        </button>
                      </TableHead>
                    ))}
                    <TableHead className="w-[28%] text-right sm:w-auto" />
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {rows.map(({ endpoint, name }) => {
                    const { credential, model, route, testable } = endpoint;
                    const key = endpointKey(endpoint);
                    const check = checks[key];
                    const result =
                      check?.status === "complete" ? check.result : null;
                    const expanded = expandedModel === key;
                    const output =
                      check?.status === "complete"
                        ? JSON.stringify(check.result, null, 2)
                        : check?.status === "error"
                          ? JSON.stringify({ error: check.message }, null, 2)
                          : "";
                    const testedAt =
                      check?.status === "complete" || check?.status === "error"
                        ? new Date(check.testedAt).toLocaleTimeString()
                        : "—";

                    return (
                      <Fragment key={key}>
                        <TableRow>
                          <TableCell className="overflow-hidden py-2">
                            <div className="flex items-center gap-3">
                              <div className="bg-background flex h-7 w-7 shrink-0 items-center justify-center rounded-md border">
                                <QueueKeyIcon queueKey={model} size={18} />
                              </div>
                              <div className="min-w-0">
                                <div
                                  className="truncate text-sm font-medium"
                                  title={name}
                                >
                                  {name}
                                </div>
                                <div
                                  className="text-muted-foreground truncate font-mono text-xs"
                                  title={model}
                                >
                                  {model}
                                </div>
                                <div className="text-muted-foreground mt-0.5 hidden text-xs sm:block lg:hidden">
                                  {ROUTE_LABELS[route] ?? route}
                                </div>
                              </div>
                            </div>
                          </TableCell>
                          <TableCell className="hidden lg:table-cell">
                            <div className="text-sm font-medium">
                              {ROUTE_LABELS[route] ?? route}
                            </div>
                          </TableCell>
                          <TableCell>
                            {check?.status === "running" ? (
                              <Badge variant="running">
                                <RefreshCw className="mr-1 hidden h-3 w-3 animate-spin sm:inline" />
                                Testing
                              </Badge>
                            ) : result ? (
                              <Badge variant={result.ok ? "success" : "failed"}>
                                {result.ok ? (
                                  <CheckCircle2 className="mr-1 hidden h-3 w-3 sm:inline" />
                                ) : (
                                  <XCircle className="mr-1 hidden h-3 w-3 sm:inline" />
                                )}
                                {result.ok
                                  ? "Passed"
                                  : result.status_code
                                    ? `HTTP ${result.status_code}`
                                    : "Failed"}
                              </Badge>
                            ) : check?.status === "error" ? (
                              <Badge variant="failed">
                                <XCircle className="mr-1 hidden h-3 w-3 sm:inline" />
                                Failed
                              </Badge>
                            ) : !testable ? (
                              <span className="text-muted-foreground text-xs">
                                CLI only
                              </span>
                            ) : (
                              <span className="text-muted-foreground text-xs">
                                Not tested
                              </span>
                            )}
                          </TableCell>
                          <TableCell className="hidden font-mono text-xs md:table-cell">
                            {result ? `${result.latency_ms}ms` : "—"}
                          </TableCell>
                          <TableCell className="text-muted-foreground hidden text-xs md:table-cell">
                            {testedAt}
                          </TableCell>
                          <TableCell className="text-right">
                            <div className="flex justify-end gap-1">
                              {check && (
                                <Button
                                  variant="ghost"
                                  size="icon"
                                  className="h-7 w-7 sm:h-9 sm:w-9"
                                  aria-label={`${expanded ? "Hide" : "Show"} ${model} result`}
                                  aria-expanded={expanded}
                                  aria-controls={`model-output-${key}`}
                                  onClick={() =>
                                    setExpandedModel(expanded ? null : key)
                                  }
                                >
                                  <ChevronDown
                                    className={`h-4 w-4 transition-transform ${expanded ? "rotate-180" : ""}`}
                                  />
                                </Button>
                              )}
                              <Button
                                variant="outline"
                                size="sm"
                                className="h-7 w-7 px-0 sm:h-8 sm:w-auto sm:px-3"
                                title={
                                  testable
                                    ? `Test ${model}`
                                    : "Requires the Cursor agent CLI"
                                }
                                disabled={hasRunningCheck || !testable}
                                onClick={() => void testModel(endpoint, true)}
                              >
                                {check?.status === "running" ? (
                                  <RefreshCw className="h-4 w-4 animate-spin" />
                                ) : (
                                  <Play className="h-4 w-4" />
                                )}
                                <span className="sr-only sm:not-sr-only">
                                  {testable ? "Test" : "CLI only"}
                                </span>
                              </Button>
                            </div>
                          </TableCell>
                        </TableRow>
                        {expanded && (
                          <TableRow className="bg-muted/20 hover:bg-muted/20">
                            <TableCell colSpan={6} className="p-0">
                              <div
                                id={`model-output-${key}`}
                                role="region"
                                aria-label={`${model} via ${route} test output`}
                                className="border-t px-4 py-3"
                              >
                                <div className="mb-2 flex items-center justify-between gap-3">
                                  <span className="text-muted-foreground text-xs font-medium tracking-wide uppercase">
                                    Output
                                  </span>
                                  <span className="text-muted-foreground font-mono text-xs">
                                    {check?.status === "running"
                                      ? "Request in progress"
                                      : result
                                        ? `${result.status_code ? `HTTP ${result.status_code} · ` : ""}${result.latency_ms}ms`
                                        : "Request failed"}
                                  </span>
                                </div>
                                {check?.status === "running" ? (
                                  <div className="text-muted-foreground flex items-center gap-2 py-2 text-sm">
                                    <RefreshCw className="h-4 w-4 animate-spin" />
                                    Waiting for response...
                                  </div>
                                ) : (
                                  <div className="space-y-3">
                                    <p className="text-sm break-words whitespace-pre-wrap">
                                      {result
                                        ? result.ok
                                          ? result.response ||
                                            "Request completed with no text response."
                                          : result.error
                                        : check?.status === "error"
                                          ? check.message
                                          : null}
                                    </p>
                                    <details>
                                      <summary className="text-muted-foreground cursor-pointer text-xs">
                                        Response details
                                      </summary>
                                      <p className="text-muted-foreground my-2 text-xs break-words">
                                        {ROUTE_LABELS[route] ?? route} ·{" "}
                                        {credential ??
                                          "Provider-managed credential"}
                                      </p>
                                      <CodeBlock
                                        code={output}
                                        language="json"
                                        maxHeight="20rem"
                                        truncateAt={0}
                                      />
                                    </details>
                                  </div>
                                )}
                              </div>
                            </TableCell>
                          </TableRow>
                        )}
                      </Fragment>
                    );
                  })}
                </TableBody>
              </Table>
            )}
          </CardContent>
        </Card>
      )}
    </div>
  );
}
