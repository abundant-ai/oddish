"use client";

import dynamic from "next/dynamic";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { ByModel, ByModelEntry } from "@/lib/types";

const MarkdownRenderer = dynamic(() =>
  import("@/components/renderers/markdown-renderer").then(
    (mod) => mod.MarkdownRenderer,
  ),
);

const BUCKET_LABELS: Record<string, string> = {
  bad: "Reward hacking",
  good: "Capability",
  all: "Overall",
};

function EntryBody({ entry }: { entry: ByModelEntry }) {
  return (
    <div className="space-y-3">
      {entry.bucket !== "all" && (
        <Badge variant="outline" className="text-[10px]">
          {BUCKET_LABELS[entry.bucket] ?? entry.bucket}
        </Badge>
      )}
      {entry.narrative.trim() && <MarkdownRenderer content={entry.narrative} />}
      {entry.relative_strengths.trim() && (
        <div>
          <div className="text-xs font-medium">Relative strengths</div>
          <MarkdownRenderer content={entry.relative_strengths} />
        </div>
      )}
      {entry.relative_weaknesses.trim() && (
        <div>
          <div className="text-xs font-medium">Relative weaknesses</div>
          <MarkdownRenderer content={entry.relative_weaknesses} />
        </div>
      )}
      {entry.distinctive_failures.length > 0 && (
        <div>
          <div className="text-xs font-medium">Distinctive failures</div>
          <ul className="list-disc pl-5 text-sm">
            {entry.distinctive_failures.map((f, i) => (
              <li key={i}>{f}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

export function ByModelView({ payload }: { payload: ByModel }) {
  // Denominators are host-computed from trial rows and are the authority on
  // which models exist; union in payload.models too so nothing the LLM did
  // describe ever disappears if it names a model missing its denominators.
  const models = Array.from(
    new Set([
      ...Object.keys(payload.denominators),
      ...payload.models.map((e) => e.model),
    ]),
  ).sort();

  return (
    <div className="space-y-4">
      {payload.comparison.trim() && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Cross-model comparison</CardTitle>
          </CardHeader>
          <CardContent>
            <MarkdownRenderer content={payload.comparison} />
          </CardContent>
        </Card>
      )}

      {models.map((model) => {
        const d = payload.denominators[model];
        const entries = payload.models.filter((e) => e.model === model);
        return (
          <Card key={model}>
            <CardHeader>
              <CardTitle className="text-base">{model}</CardTitle>
              {d && (
                <div className="text-muted-foreground flex flex-wrap gap-2 text-xs">
                  <span>{d.trials} trials</span>
                  <span>
                    · {d.solved}/{d.scored} solved
                  </span>
                  <span>
                    ·{" "}
                    {d.mean_reward === null
                      ? "mean reward n/a"
                      : `mean reward ${d.mean_reward.toFixed(2)}`}
                  </span>
                  <span>
                    · {d.bad} bad / {d.good} good
                  </span>
                </div>
              )}
            </CardHeader>
            <CardContent className="space-y-4">
              {entries.length > 0 ? (
                entries.map((entry, i) => <EntryBody key={i} entry={entry} />)
              ) : (
                <p className="text-muted-foreground text-sm">
                  No per-model narrative was produced for this model.
                </p>
              )}
            </CardContent>
          </Card>
        );
      })}
    </div>
  );
}
