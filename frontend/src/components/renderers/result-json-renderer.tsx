import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { CheckCircle, CircleDashed, XCircle, Clock } from "lucide-react";
import { getRewardStyle } from "@/lib/status-config";

interface ResultJsonRendererProps {
  content: string;
}

interface Interval {
  started_at: string;
  finished_at: string;
}

/** Shape of Harbor's `result.json` (fields we surface; the rest is ignored). */
interface ResultData {
  task_name: string;
  agent_info?: {
    name: string;
    version?: string;
    model_info?: string | null;
  };
  agent_result?: {
    n_input_tokens?: number | null;
    n_cache_tokens?: number | null;
    n_output_tokens?: number | null;
    cost_usd?: number | null;
  };
  verifier_result?: {
    rewards?: Record<string, unknown>;
  };
  exception_info?: unknown;
  started_at?: string;
  finished_at?: string;
  environment_setup?: Interval;
  agent_setup?: Interval;
  agent_execution?: Interval;
  verifier?: Interval;
}

/**
 * Mirrors `_extract_reward` in oddish/core/harbor_artifacts.py: prefer the
 * `reward` key, but fall back to the sole value when a task uses a single
 * custom reward key.
 */
function extractReward(rewards?: Record<string, unknown>): number | null {
  if (!rewards || typeof rewards !== "object") return null;
  let value: unknown = rewards.reward;
  const values = Object.values(rewards);
  if (value == null && values.length === 1) value = values[0];
  const num = typeof value === "string" ? Number(value) : value;
  return typeof num === "number" && Number.isFinite(num) ? num : null;
}

function formatDuration(start?: string, end?: string): string {
  if (!start || !end) return "N/A";
  const diff = new Date(end).getTime() - new Date(start).getTime();
  return `${(diff / 1000).toFixed(2)}s`;
}

function formatDateTime(date?: string): string {
  if (!date) return "N/A";
  return new Date(date).toLocaleString();
}

/** Structured summary view for Harbor `result.json` files. */
export function ResultJsonRenderer({ content }: ResultJsonRendererProps) {
  let data: ResultData;
  try {
    data = JSON.parse(content) as ResultData;
  } catch (error) {
    return (
      <div className="text-destructive p-4">
        Failed to parse result.json:{" "}
        {error instanceof Error ? error.message : "Unknown error"}
      </div>
    );
  }

  const reward = extractReward(data.verifier_result?.rewards);
  const passed = reward === 1.0;
  const failed = reward === 0.0;
  const partial = reward != null && reward > 0 && reward < 1;
  const hasException =
    data.exception_info !== null && data.exception_info !== undefined;

  return (
    <div className="space-y-6 p-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-foreground mb-2 text-2xl font-bold">
            {data.task_name}
          </h1>
          {data.agent_info && (
            <div className="flex items-center gap-2">
              <Badge variant="outline" className="font-mono text-xs">
                {data.agent_info.name}
              </Badge>
              {data.agent_info.version && (
                <Badge variant="secondary" className="text-xs">
                  v{data.agent_info.version}
                </Badge>
              )}
            </div>
          )}
        </div>
        <div className="flex items-center gap-2">
          {hasException ? (
            <Badge variant="destructive" className="px-3 py-1 text-sm">
              <XCircle className="mr-1 h-4 w-4" />
              Exception
            </Badge>
          ) : passed ? (
            <Badge className="bg-emerald-500/90 px-3 py-1 text-sm text-white hover:bg-emerald-500">
              <CheckCircle className="mr-1 h-4 w-4" />
              PASS
            </Badge>
          ) : failed ? (
            <Badge variant="destructive" className="px-3 py-1 text-sm">
              <XCircle className="mr-1 h-4 w-4" />
              FAIL
            </Badge>
          ) : partial ? (
            <Badge
              variant="outline"
              className="px-3 py-1 text-sm"
              style={getRewardStyle(reward, "badge")}
            >
              <CircleDashed className="mr-1 h-4 w-4" />
              PARTIAL
            </Badge>
          ) : (
            <Badge variant="outline" className="px-3 py-1 text-sm">
              Unknown
            </Badge>
          )}
          {reward !== null && (
            <Badge variant="secondary" className="font-mono text-sm">
              Reward: {Number(reward.toFixed(3))}
            </Badge>
          )}
        </div>
      </div>

      <Card className="border-border bg-card">
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-base">
            <Clock className="h-4 w-4" />
            Execution Timeline
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="grid grid-cols-2 gap-4">
            <div>
              <div className="text-muted-foreground mb-1 text-xs">
                Total Duration
              </div>
              <div className="font-mono text-sm">
                {formatDuration(data.started_at, data.finished_at)}
              </div>
            </div>
            <div>
              <div className="text-muted-foreground mb-1 text-xs">
                Started At
              </div>
              <div className="font-mono text-xs">
                {formatDateTime(data.started_at)}
              </div>
            </div>
          </div>

          <div className="border-border space-y-2 border-t pt-3">
            <div className="grid grid-cols-2 gap-4 text-sm md:grid-cols-4">
              <div>
                <div className="text-muted-foreground mb-1 text-xs">
                  Environment Setup
                </div>
                <div className="font-mono text-xs">
                  {formatDuration(
                    data.environment_setup?.started_at,
                    data.environment_setup?.finished_at
                  )}
                </div>
              </div>
              <div>
                <div className="text-muted-foreground mb-1 text-xs">
                  Agent Setup
                </div>
                <div className="font-mono text-xs">
                  {formatDuration(
                    data.agent_setup?.started_at,
                    data.agent_setup?.finished_at
                  )}
                </div>
              </div>
              <div>
                <div className="text-muted-foreground mb-1 text-xs">
                  Agent Execution
                </div>
                <div className="font-mono text-xs">
                  {formatDuration(
                    data.agent_execution?.started_at,
                    data.agent_execution?.finished_at
                  )}
                </div>
              </div>
              <div>
                <div className="text-muted-foreground mb-1 text-xs">
                  Verification
                </div>
                <div className="font-mono text-xs">
                  {formatDuration(
                    data.verifier?.started_at,
                    data.verifier?.finished_at
                  )}
                </div>
              </div>
            </div>
          </div>
        </CardContent>
      </Card>

      {data.agent_result && (
        <Card className="border-border bg-card">
          <CardHeader>
            <CardTitle className="text-base">Agent Metrics</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
              {data.agent_result.n_input_tokens != null && (
                <div>
                  <div className="text-muted-foreground mb-1 text-xs">
                    Input Tokens
                  </div>
                  <div className="font-mono text-sm">
                    {data.agent_result.n_input_tokens.toLocaleString()}
                  </div>
                </div>
              )}
              {data.agent_result.n_output_tokens != null && (
                <div>
                  <div className="text-muted-foreground mb-1 text-xs">
                    Output Tokens
                  </div>
                  <div className="font-mono text-sm">
                    {data.agent_result.n_output_tokens.toLocaleString()}
                  </div>
                </div>
              )}
              {data.agent_result.n_cache_tokens != null && (
                <div>
                  <div className="text-muted-foreground mb-1 text-xs">
                    Cache Tokens
                  </div>
                  <div className="font-mono text-sm">
                    {data.agent_result.n_cache_tokens.toLocaleString()}
                  </div>
                </div>
              )}
              {data.agent_result.cost_usd != null && (
                <div>
                  <div className="text-muted-foreground mb-1 text-xs">Cost</div>
                  <div className="font-mono text-sm">
                    ${data.agent_result.cost_usd.toFixed(4)}
                  </div>
                </div>
              )}
            </div>
          </CardContent>
        </Card>
      )}

      {hasException && (
        <Card className="border-destructive/50 bg-destructive/10">
          <CardHeader>
            <CardTitle className="text-destructive text-base">
              Exception Details
            </CardTitle>
          </CardHeader>
          <CardContent>
            <pre className="text-destructive overflow-auto font-mono text-xs">
              {JSON.stringify(data.exception_info, null, 2)}
            </pre>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
