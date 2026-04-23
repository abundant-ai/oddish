"use client";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";

interface ConfigData {
  task?: {
    path?: string;
    git_url?: string | null;
    git_commit_id?: string | null;
    overwrite?: boolean;
    download_dir?: string | null;
    source?: string;
  };
  trial_name?: string;
  trials_dir?: string;
  timeout_multiplier?: number;
  agent?: {
    name?: string;
    import_path?: string | null;
    model_name?: string | null;
    override_timeout_sec?: number | null;
    kwargs?: Record<string, unknown>;
  };
  environment?: {
    type?: string;
    force_build?: boolean;
    delete?: boolean;
    kwargs?: Record<string, unknown>;
  };
  verifier?: {
    override_timeout_sec?: number | null;
  };
  job_id?: string;
}

export function ConfigJsonRenderer({ content }: { content: string }) {
  let data: ConfigData;
  try {
    data = JSON.parse(content);
  } catch {
    return (
      <div className="p-4 text-destructive">Failed to parse config.json</div>
    );
  }

  return (
    <div className="space-y-3 p-3 text-sm">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h2 className="text-base font-semibold text-foreground">
            Workflow Configuration
          </h2>
          {data.job_id && (
            <p className="mt-0.5 font-mono text-xs text-muted-foreground">
              Job ID: {data.job_id}
            </p>
          )}
        </div>
      </div>

      {data.task && (
        <Card className="border-border/50 bg-card/50">
          <CardHeader>
            <CardTitle className="text-sm">Task Configuration</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            {data.task.path && (
              <Row label="Path">
                <span className="font-mono text-xs text-foreground">
                  {data.task.path}
                </span>
              </Row>
            )}
            {data.task.source && (
              <Row label="Source">
                <Badge variant="secondary" className="text-xs">
                  {data.task.source}
                </Badge>
              </Row>
            )}
            {data.task.git_url && (
              <Row label="Git URL">
                <span className="font-mono text-xs text-foreground">
                  {data.task.git_url}
                </span>
              </Row>
            )}
            {data.task.git_commit_id && (
              <Row label="Commit">
                <span className="font-mono text-xs text-foreground">
                  {data.task.git_commit_id}
                </span>
              </Row>
            )}
            {data.task.overwrite !== undefined && (
              <Row label="Overwrite">
                <Badge
                  variant={data.task.overwrite ? "default" : "secondary"}
                  className="text-xs"
                >
                  {data.task.overwrite ? "Yes" : "No"}
                </Badge>
              </Row>
            )}
          </CardContent>
        </Card>
      )}

      {(data.trial_name || data.trials_dir || data.timeout_multiplier) && (
        <Card className="border-border/50 bg-card/50">
          <CardHeader>
            <CardTitle className="text-sm">Trial Information</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            {data.trial_name && (
              <Row label="Name">
                <span className="font-mono text-xs text-foreground">
                  {data.trial_name}
                </span>
              </Row>
            )}
            {data.trials_dir && (
              <Row label="Directory">
                <span className="font-mono text-xs text-foreground">
                  {data.trials_dir}
                </span>
              </Row>
            )}
            {data.timeout_multiplier !== undefined && (
              <Row label="Timeout Multiplier">
                <Badge variant="secondary" className="text-xs">
                  {data.timeout_multiplier}x
                </Badge>
              </Row>
            )}
          </CardContent>
        </Card>
      )}

      {data.agent && (
        <Card className="border-border/50 bg-card/50">
          <CardHeader>
            <CardTitle className="text-sm">Agent Configuration</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            {data.agent.name && (
              <Row label="Name">
                <Badge variant="default" className="text-xs">
                  {data.agent.name}
                </Badge>
              </Row>
            )}
            {data.agent.model_name && (
              <Row label="Model">
                <span className="font-mono text-xs text-foreground">
                  {data.agent.model_name}
                </span>
              </Row>
            )}
            {data.agent.import_path && (
              <Row label="Import Path">
                <span className="font-mono text-xs text-foreground">
                  {data.agent.import_path}
                </span>
              </Row>
            )}
            {data.agent.override_timeout_sec !== null &&
              data.agent.override_timeout_sec !== undefined && (
                <Row label="Timeout Override">
                  <Badge variant="secondary" className="text-xs">
                    {data.agent.override_timeout_sec}s
                  </Badge>
                </Row>
              )}
            {data.agent.kwargs && Object.keys(data.agent.kwargs).length > 0 && (
              <Row label="Arguments">
                <pre className="rounded bg-muted/50 p-2 font-mono text-xs text-foreground">
                  {JSON.stringify(data.agent.kwargs, null, 2)}
                </pre>
              </Row>
            )}
          </CardContent>
        </Card>
      )}

      {data.environment && (
        <Card className="border-border/50 bg-card/50">
          <CardHeader>
            <CardTitle className="text-sm">Environment Configuration</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            {data.environment.type && (
              <Row label="Type">
                <Badge variant="secondary" className="text-xs">
                  {data.environment.type}
                </Badge>
              </Row>
            )}
            {data.environment.force_build !== undefined && (
              <Row label="Force Build">
                <Badge
                  variant={
                    data.environment.force_build ? "default" : "secondary"
                  }
                  className="text-xs"
                >
                  {data.environment.force_build ? "Yes" : "No"}
                </Badge>
              </Row>
            )}
            {data.environment.delete !== undefined && (
              <Row label="Delete After">
                <Badge
                  variant={data.environment.delete ? "default" : "secondary"}
                  className="text-xs"
                >
                  {data.environment.delete ? "Yes" : "No"}
                </Badge>
              </Row>
            )}
            {data.environment.kwargs &&
              Object.keys(data.environment.kwargs).length > 0 && (
                <Row label="Arguments">
                  <pre className="rounded bg-muted/50 p-2 font-mono text-xs text-foreground">
                    {JSON.stringify(data.environment.kwargs, null, 2)}
                  </pre>
                </Row>
              )}
          </CardContent>
        </Card>
      )}

      {data.verifier && (
        <Card className="border-border/50 bg-card/50">
          <CardHeader>
            <CardTitle className="text-sm">Verifier Configuration</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            {data.verifier.override_timeout_sec !== null &&
              data.verifier.override_timeout_sec !== undefined && (
                <Row label="Timeout Override">
                  <Badge variant="secondary" className="text-xs">
                    {data.verifier.override_timeout_sec}s
                  </Badge>
                </Row>
              )}
          </CardContent>
        </Card>
      )}
    </div>
  );
}

function Row({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex items-start gap-2">
      <span className="min-w-[110px] text-xs text-muted-foreground">
        {label}:
      </span>
      {children}
    </div>
  );
}
