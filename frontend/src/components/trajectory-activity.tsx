"use client";

import { useRef, useState } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Activity, ImageDown, Star } from "lucide-react";
import type { TrajectoryStep } from "@/lib/types";
import {
  downloadActivityImage,
  type ActivityImageFormat,
} from "@/lib/activity-export";
import {
  buildActivityViewModel,
  STEP_BAND_TITLE,
  TIMELINE_CAPTION,
  TOKEN_BAND_TITLE,
} from "@/lib/activity-view-model";
import { useTrajectorySummary } from "@/lib/use-trajectory-summary";

interface TrajectoryActivityProps {
  trialId: string;
  steps: TrajectoryStep[];
  apiBaseUrl?: string;
  stepIdToIndex: (stepId: number) => number;
  onStepSelect: (index: number) => void;
}

export function TrajectoryActivity({
  trialId,
  steps,
  apiBaseUrl = "/api",
  stepIdToIndex,
  onStepSelect,
}: TrajectoryActivityProps) {
  const { data } = useTrajectorySummary(trialId, apiBaseUrl);
  const cardRef = useRef<HTMLDivElement>(null);
  const [exportError, setExportError] = useState<string | null>(null);

  const model = buildActivityViewModel(steps, data);
  if (!model) return null;

  const select = (stepId: number) => {
    const idx = stepIdToIndex(stepId);
    if (idx >= 0) onStepSelect(idx);
  };

  const exportImage = async (format: ActivityImageFormat) => {
    const host = cardRef.current;
    if (!host) return;
    try {
      setExportError(null);
      await downloadActivityImage({ host, model, trialId, format });
    } catch (err) {
      setExportError(err instanceof Error ? err.message : "Export failed");
    }
  };

  return (
    <Card className="my-3" ref={cardRef}>
      <CardHeader className="pb-2">
        <CardTitle className="flex items-center justify-between text-sm font-medium">
          <span className="flex items-center gap-2">
            <Activity className="h-4 w-4" />
            Activity
          </span>
          <DropdownMenu modal={false}>
            <DropdownMenuTrigger asChild>
              <Button
                type="button"
                variant="ghost"
                size="sm"
                aria-label="Export activity as image"
                className="text-muted-foreground hover:text-foreground h-7 w-7 p-0"
              >
                <ImageDown className="h-3.5 w-3.5" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="w-[110px]">
              <DropdownMenuItem
                className="text-xs"
                onSelect={() => void exportImage("png")}
              >
                PNG
              </DropdownMenuItem>
              <DropdownMenuItem
                className="text-xs"
                onSelect={() => void exportImage("jpeg")}
              >
                JPEG
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        {exportError && (
          <p className="text-destructive text-xs">{exportError}</p>
        )}
        {/* one mini bar chart per metric; rows double as the timeline legend */}
        {model.sections.map((section) => (
          <div key={section.name}>
            <div className="flex items-baseline justify-between">
              <span className="text-xs font-medium">{section.name}</span>
              <span className="text-muted-foreground font-mono text-xs">
                {section.totalLabel} total
              </span>
            </div>
            <div className="mt-1.5 space-y-1">
              {section.rows.map((row) => (
                <div key={row.key} className="flex items-center gap-2">
                  <span
                    className="flex w-36 shrink-0 items-center gap-1.5 text-xs"
                    title={row.label}
                  >
                    <span
                      className="h-3 w-1 shrink-0 rounded-sm"
                      style={{ background: row.color }}
                    />
                    <span className="truncate">{row.label}</span>
                  </span>
                  {/* gap between segments = the split between instances */}
                  <div className="bg-muted/40 flex h-2 flex-1 gap-0.5 overflow-hidden rounded-sm">
                    {row.bars.map((bar, i) => (
                      <button
                        key={`${bar.firstStepId}-${i}`}
                        type="button"
                        title={bar.title}
                        onClick={() => select(bar.firstStepId)}
                        style={{
                          width: `${bar.pct}%`,
                          background: row.color,
                        }}
                        className="h-full min-w-[3px] shrink-0 rounded-sm transition hover:brightness-110"
                      />
                    ))}
                  </div>
                  <span className="text-muted-foreground w-16 shrink-0 text-right font-mono text-xs">
                    {row.valueLabel}
                  </span>
                </div>
              ))}
            </div>
          </div>
        ))}

        {/* timeline: the same runs measured two ways */}
        <div className="overflow-x-auto">
          <div className="min-w-[560px]">
            <div className="flex items-baseline justify-between">
              <span className="text-xs font-medium">{STEP_BAND_TITLE}</span>
              <span className="text-muted-foreground font-mono text-xs">
                {model.stepTotalLabel}
              </span>
            </div>
            <div className="mt-1.5 flex h-10 items-stretch gap-0.5">
              {model.runs.map((run, r) => (
                <div
                  key={r}
                  className="flex overflow-hidden rounded-sm"
                  style={{ flex: `${run.stepWeight} 1 0` }}
                >
                  {run.steps.map((step) => (
                    <button
                      key={step.stepId}
                      type="button"
                      title={step.title}
                      onClick={() => select(step.stepId)}
                      style={{ flex: "1 1 0", background: run.color }}
                      className="focus-visible:outline-ring relative min-w-px outline-offset-2 transition hover:brightness-110 focus-visible:outline focus-visible:outline-2"
                    >
                      {step.highlighted && (
                        <Star className="absolute top-0.5 right-0.5 h-3 w-3 fill-white text-white drop-shadow" />
                      )}
                    </button>
                  ))}
                </div>
              ))}
            </div>

            {model.hasTokens && (
              <>
                {/* Per-step token intensity, laid out against the step band
                    above so the columns line up. Answers "which individual
                    steps were heavy", which the aggregate band cannot. */}
                <div className="mt-1 flex h-3 gap-0.5">
                  {model.runs.map((run, r) => (
                    <div
                      key={r}
                      className="flex overflow-hidden rounded-sm"
                      style={{ flex: `${run.stepWeight} 1 0` }}
                    >
                      {run.steps.map((step) => (
                        <button
                          key={step.stepId}
                          type="button"
                          title={step.heatTitle}
                          onClick={() => select(step.stepId)}
                          style={{ flex: "1 1 0", background: step.heatColor }}
                          className="min-w-px"
                        />
                      ))}
                    </div>
                  ))}
                </div>

                {/* the same runs again, sized by tokens not step count */}
                <div className="mt-3 flex items-baseline justify-between">
                  <span className="text-xs font-medium">
                    {TOKEN_BAND_TITLE}
                  </span>
                  <span className="text-muted-foreground font-mono text-xs">
                    {model.tokenTotalLabel}
                  </span>
                </div>
                <div className="mt-1.5 flex h-10 gap-0.5">
                  {model.runs.map((run, r) => (
                    <div
                      key={r}
                      className="flex overflow-hidden rounded-sm"
                      style={{ flex: `${run.tokenWeight} 1 0` }}
                    >
                      {run.steps.map((step) => (
                        <button
                          key={step.stepId}
                          type="button"
                          title={step.tokenTitle}
                          onClick={() => select(step.stepId)}
                          style={{
                            flex: `${step.tokenFlex} 1 0`,
                            background: run.color,
                          }}
                          className="min-w-px transition hover:brightness-110"
                        />
                      ))}
                    </div>
                  ))}
                </div>
              </>
            )}
            {model.hasTokens && (
              <p className="text-muted-foreground mt-2 font-mono text-[10.5px]">
                {TIMELINE_CAPTION}
              </p>
            )}
          </div>
        </div>

        {/* Without this the totals here silently disagree with the step count
            shown for the same trial everywhere else. */}
        {model.emptyNote && (
          <p className="text-muted-foreground font-mono text-[10.5px]">
            {model.emptyNote}
          </p>
        )}
      </CardContent>
    </Card>
  );
}
