"use client";

import { Fragment, useState } from "react";
import useSWR from "swr";
import {
  AlertCircle,
  CheckCircle2,
  Play,
  RefreshCw,
  XCircle,
} from "lucide-react";

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
import { fetcher } from "@/lib/api";
import type {
  ModelEndpointCatalogResponse,
  ModelEndpointCheckResponse,
  ModelEndpointSummary,
} from "@/lib/types";

type ModelCheckState =
  | { status: "running" }
  | {
      status: "complete";
      result: ModelEndpointCheckResponse;
      testedAt: number;
    }
  | { status: "error"; message: string; testedAt: number };

const MODEL_CHECK_BATCH_SIZE = 3;

const ROUTE_LABELS: Record<string, string> = {
  "anthropic-hdo": "Anthropic HDO",
  anthropic: "Anthropic",
  azure: "Azure OpenAI",
  bedrock: "AWS Bedrock",
  deepseek: "DeepSeek",
  fireworks: "Fireworks",
  gemini: "Google Gemini",
  meta: "Meta",
  minimax: "MiniMax",
  moonshot: "Moonshot",
  openai: "OpenAI",
  openrouter: "OpenRouter",
  xai: "xAI",
  zai: "Z.ai",
};

function endpointKey(endpoint: ModelEndpointSummary): string {
  return `${endpoint.route}:${endpoint.model}`;
}

export function ModelsClient() {
  const { data, error, isLoading, mutate } =
    useSWR<ModelEndpointCatalogResponse>("/api/models", fetcher);
  const [checks, setChecks] = useState<Record<string, ModelCheckState>>({});
  const [expandedModel, setExpandedModel] = useState<string | null>(null);

  const hasRunningCheck = Object.values(checks).some(
    (check) => check.status === "running"
  );
  const passing = Object.values(checks).filter(
    (check) => check.status === "complete" && check.result.ok
  ).length;
  const failing = Object.values(checks).filter(
    (check) =>
      check.status === "error" ||
      (check.status === "complete" && !check.result.ok)
  ).length;
  const providerCount = new Set(data?.models.map(({ route }) => route)).size;

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
      const result = await fetcher<ModelEndpointCheckResponse>("/api/models", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model: endpoint.model, route: endpoint.route }),
      });
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

  async function testAllModels() {
    if (!data?.models.length) return;
    const outcomes: { key: string; ok: boolean }[] = [];
    for (
      let index = 0;
      index < data.models.length;
      index += MODEL_CHECK_BATCH_SIZE
    ) {
      const batch = data.models.slice(index, index + MODEL_CHECK_BATCH_SIZE);
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
  }

  return (
    <div className="space-y-5">
      <div className="flex flex-col justify-between gap-3 sm:flex-row sm:items-end">
        <div>
          <h1 className="text-2xl font-bold">Models</h1>
          <p className="text-muted-foreground mt-1 text-sm">
            Send a direct provider completion with Oddish platform credentials.
            No task, worker, agent CLI, or sandbox is created.
          </p>
        </div>
        <Button
          variant="outline"
          disabled={!data?.allowed || !data.models.length || hasRunningCheck}
          onClick={() => void testAllModels()}
        >
          {hasRunningCheck ? (
            <RefreshCw className="h-4 w-4 animate-spin" />
          ) : (
            <Play className="h-4 w-4" />
          )}
          Test all
        </Button>
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
            <div className="flex items-center justify-between gap-3">
              <CardTitle className="text-base">Available models</CardTitle>
              <span className="text-muted-foreground text-sm">
                {data?.models.length ?? 0} models · {providerCount} providers
                {(passing > 0 || failing > 0) &&
                  ` · ${passing} passing · ${failing} failing`}
              </span>
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
            ) : (
              <Table className="table-fixed sm:table-auto">
                <TableHeader>
                  <TableRow>
                    <TableHead className="w-[48%] sm:w-auto">Model</TableHead>
                    <TableHead className="hidden lg:table-cell">
                      Provider
                    </TableHead>
                    <TableHead className="w-[28%] sm:w-auto">Status</TableHead>
                    <TableHead className="hidden md:table-cell">
                      Latency
                    </TableHead>
                    <TableHead className="hidden md:table-cell">
                      Tested
                    </TableHead>
                    <TableHead className="w-[24%] text-right sm:w-auto" />
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {data.models.map((endpoint) => {
                    const { credential, model, provider, route } = endpoint;
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
                          <TableCell className="overflow-hidden py-3">
                            <div className="flex items-center gap-3">
                              <div className="bg-background flex h-9 w-9 shrink-0 items-center justify-center rounded-md border">
                                <QueueKeyIcon queueKey={model} size={18} />
                              </div>
                              <div className="min-w-0">
                                <div className="truncate font-mono text-sm font-medium">
                                  {model}
                                </div>
                                <div className="text-muted-foreground mt-0.5 hidden text-xs sm:block">
                                  <span className="lg:hidden">
                                    {ROUTE_LABELS[route] ?? route}
                                    {credential ? ` · ${credential}` : ""}{" "}
                                    ·{" "}
                                  </span>
                                  {provider} model · litellm completion
                                </div>
                              </div>
                            </div>
                          </TableCell>
                          <TableCell className="hidden lg:table-cell">
                            <div className="text-sm font-medium">
                              {ROUTE_LABELS[route] ?? route}
                            </div>
                            <div className="text-muted-foreground mt-0.5 font-mono text-xs">
                              {credential ?? "Provider-managed credential"}
                            </div>
                          </TableCell>
                          <TableCell>
                            {check?.status === "running" ? (
                              <Badge variant="running">
                                <RefreshCw className="mr-1 h-3 w-3 animate-spin" />
                                Testing
                              </Badge>
                            ) : result ? (
                              <Badge variant={result.ok ? "success" : "failed"}>
                                {result.ok ? (
                                  <CheckCircle2 className="mr-1 h-3 w-3" />
                                ) : (
                                  <XCircle className="mr-1 h-3 w-3" />
                                )}
                                {result.ok
                                  ? "Passed"
                                  : result.status_code
                                    ? `HTTP ${result.status_code}`
                                    : "Failed"}
                              </Badge>
                            ) : check?.status === "error" ? (
                              <Badge variant="failed">
                                <XCircle className="mr-1 h-3 w-3" />
                                Failed
                              </Badge>
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
                            <Button
                              variant="outline"
                              size="sm"
                              disabled={hasRunningCheck}
                              aria-expanded={expanded}
                              aria-controls={`model-output-${key}`}
                              onClick={() => void testModel(endpoint, true)}
                            >
                              {check?.status === "running" ? (
                                <RefreshCw className="h-4 w-4 animate-spin" />
                              ) : (
                                <Play className="h-4 w-4" />
                              )}
                              Test
                            </Button>
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
                                  <pre className="max-h-80 overflow-auto rounded-md border bg-black/30 p-3 font-mono text-xs leading-relaxed whitespace-pre-wrap">
                                    <code>{output}</code>
                                  </pre>
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
