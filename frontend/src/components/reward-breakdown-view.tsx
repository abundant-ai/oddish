"use client";

import { useEffect, useRef, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  XCircle,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { formatRewardValue, getRewardStyle } from "@/lib/status-config";
import {
  splitRewardsMap,
  type RewardBreakdown,
  type RewardCriterionResult,
  type RewardDimensionResult,
  type RewardsMap,
} from "@/lib/reward-kit";

function scoreTone(value: number): "pass" | "fail" | "partial" {
  if (value >= 1) return "pass";
  if (value <= 0) return "fail";
  return "partial";
}

function ScorePill({ value }: { value: number }) {
  const tone = scoreTone(value);
  return (
    <span
      className={cn(
        "shrink-0 rounded border px-1.5 py-px font-mono text-[11px] font-semibold tabular-nums",
        tone === "pass" &&
          "border-emerald-500/30 bg-emerald-500/10 text-emerald-600 dark:text-emerald-400",
        tone === "fail" &&
          "border-red-500/30 bg-red-500/10 text-red-600 dark:text-red-400"
      )}
      style={tone === "partial" ? getRewardStyle(value, "badge") : undefined}
    >
      {formatRewardValue(value)}
    </span>
  );
}

function KindBadge({ dimension }: { dimension: RewardDimensionResult }) {
  if (dimension.kind === "llm" || dimension.kind === "agent") {
    return (
      <span className="shrink-0 rounded border border-blue-500/30 bg-blue-500/10 px-1.5 py-px font-mono text-[10px] text-blue-600 dark:text-blue-400">
        {dimension.kind === "agent" ? "agent judge" : "LLM judge"}
      </span>
    );
  }
  if (dimension.kind === "programmatic") {
    return (
      <span className="text-muted-foreground shrink-0 rounded border px-1.5 py-px font-mono text-[10px]">
        programmatic
      </span>
    );
  }
  return null;
}

function formatRaw(raw: unknown): string | null {
  if (raw === undefined || raw === null) return null;
  if (typeof raw === "string") return raw;
  if (typeof raw === "number" || typeof raw === "boolean") return String(raw);
  try {
    return JSON.stringify(raw);
  } catch {
    return null;
  }
}

function CriterionRow({
  criterion,
  judged,
  criterionKey,
  selectedCriterion,
  onSelectCriterion,
}: {
  criterion: RewardCriterionResult;
  judged: boolean;
  criterionKey: string;
  selectedCriterion?: string | null;
  onSelectCriterion?: (key: string | null) => void;
}) {
  // Controlled when the host addresses criteria (the drawer's ?file=
  // scoping); local state otherwise (file renderers).
  const controlled = onSelectCriterion !== undefined;
  const [localExpanded, setLocalExpanded] = useState(false);
  const expanded = controlled
    ? selectedCriterion === criterionKey
    : localExpanded;
  const toggle = () => {
    if (controlled) {
      onSelectCriterion(expanded ? null : criterionKey);
    } else {
      setLocalExpanded((v) => !v);
    }
  };
  const raw = formatRaw(criterion.raw);
  const hasDetail = Boolean(
    criterion.description || criterion.reasoning || criterion.error || raw
  );
  const tone = scoreTone(criterion.value);

  return (
    <div
      className="border-border/60 border-t first:border-t-0"
      data-criterion={criterionKey}
    >
      <button
        type="button"
        onClick={() => hasDetail && toggle()}
        className={cn(
          "flex w-full items-center gap-2 px-3 py-1.5 text-left",
          hasDetail && "hover:bg-muted/40 cursor-pointer"
        )}
        aria-expanded={hasDetail ? expanded : undefined}
      >
        {hasDetail ? (
          expanded ? (
            <ChevronDown className="text-muted-foreground h-3 w-3 shrink-0" />
          ) : (
            <ChevronRight className="text-muted-foreground h-3 w-3 shrink-0" />
          )
        ) : (
          <span className="w-3 shrink-0" />
        )}
        {tone === "pass" ? (
          <CheckCircle2 className="h-3.5 w-3.5 shrink-0 text-emerald-500" />
        ) : tone === "fail" ? (
          <XCircle className="h-3.5 w-3.5 shrink-0 text-red-500" />
        ) : (
          <span className="h-3.5 w-3.5 shrink-0 rounded-full border-2 border-amber-500" />
        )}
        <span className="min-w-0 flex-1 truncate font-mono text-[11.5px]">
          {criterion.name}
          {criterion.negate && (
            <span className="text-muted-foreground ml-1 text-[10px]">
              (negated)
            </span>
          )}
          {criterion.optional && (
            <span className="text-muted-foreground ml-1 text-[10px]">
              (optional)
            </span>
          )}
        </span>
        {criterion.weight !== 1 && (
          <span className="text-muted-foreground shrink-0 font-mono text-[10px] tabular-nums">
            w {criterion.weight}
          </span>
        )}
        <ScorePill value={criterion.value} />
      </button>
      {expanded && hasDetail && (
        <div className="space-y-2 px-8 pt-0.5 pb-2.5">
          {criterion.description && (
            <p className="text-muted-foreground text-[11.5px]">
              {criterion.description}
            </p>
          )}
          {raw !== null && (
            <div className="text-[11px]">
              <span className="text-muted-foreground">
                {judged ? "judge answered" : "check returned"}:{" "}
              </span>
              <span className="bg-muted rounded px-1 py-px font-mono">
                {raw}
              </span>
              <span className="text-muted-foreground">
                {" "}
                → scored {formatRewardValue(criterion.value)}
              </span>
            </div>
          )}
          {criterion.reasoning && (
            <div className="border-border rounded border-l-2 pl-2 text-[11.5px]">
              <span className="text-muted-foreground text-[10px] font-semibold tracking-wider uppercase">
                Reasoning
              </span>
              <p className="mt-0.5 whitespace-pre-wrap">
                {criterion.reasoning}
              </p>
            </div>
          )}
          {criterion.error && (
            <div className="flex items-start gap-1.5 text-[11.5px] text-red-600 dark:text-red-400">
              <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" />
              <span className="font-mono whitespace-pre-wrap">
                {criterion.error}
              </span>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function DimensionSection({
  dimension,
  selectedCriterion,
  onSelectCriterion,
}: {
  dimension: RewardDimensionResult;
  selectedCriterion?: string | null;
  onSelectCriterion?: (key: string | null) => void;
}) {
  const judged = dimension.kind === "llm" || dimension.kind === "agent";
  const inputs = [
    ...(dimension.judgeFiles ?? []),
    ...(dimension.judgeTrajectory ? [dimension.judgeTrajectory] : []),
  ];

  return (
    <div className="border-border overflow-hidden rounded-md border">
      <div className="bg-muted/40 flex items-center gap-2 px-3 py-1.5">
        <span className="min-w-0 flex-1 truncate font-mono text-[12px] font-semibold">
          {dimension.name}
        </span>
        <KindBadge dimension={dimension} />
        {dimension.judge && (
          <span
            className="text-muted-foreground max-w-40 truncate font-mono text-[10px]"
            title={dimension.judge}
          >
            {dimension.judge}
          </span>
        )}
        {typeof dimension.warningsCount === "number" &&
          dimension.warningsCount > 0 && (
            <span
              className="flex items-center gap-0.5 text-[10px] text-amber-600 dark:text-amber-400"
              title={dimension.warnings?.join("\n")}
            >
              <AlertTriangle className="h-3 w-3" />
              {dimension.warningsCount}
            </span>
          )}
        <ScorePill value={dimension.score} />
      </div>
      {inputs.length > 0 && (
        <div className="border-border/60 flex flex-wrap items-center gap-1 border-t px-3 py-1">
          <span className="text-muted-foreground text-[10px] font-semibold tracking-wider uppercase">
            Judge read
          </span>
          {inputs.map((input) => (
            <span
              key={input}
              className="bg-muted text-muted-foreground rounded px-1.5 py-px font-mono text-[10px]"
            >
              {input}
            </span>
          ))}
        </div>
      )}
      {dimension.criteria.map((criterion, index) => (
        <CriterionRow
          key={`${criterion.name}-${index}`}
          criterion={criterion}
          judged={judged}
          criterionKey={`${dimension.name}/${criterion.name}`}
          selectedCriterion={selectedCriterion}
          onSelectCriterion={onSelectCriterion}
        />
      ))}
      {typeof dimension.criteriaTotal === "number" && (
        <div className="text-muted-foreground border-border/60 border-t px-3 py-1 text-[10.5px]">
          {dimension.criteriaTotal - dimension.criteria.length} more criteria in
          the full report
        </div>
      )}
    </div>
  );
}

export interface RewardBreakdownViewProps {
  breakdown: RewardBreakdown | null;
  rewards: RewardsMap | null;
  className?: string;
  /**
   * Addressed criterion as ``dimension/criterion`` — the drawer's
   * ``?file=`` value while ``?tab=rewards``. When provided together with
   * ``onSelectCriterion``, expansion is controlled and every expanded
   * criterion is a shareable address.
   */
  selectedCriterion?: string | null;
  onSelectCriterion?: (key: string | null) => void;
}

/**
 * The RewardKit result tree: named aggregate scores, then each dimension with
 * its criteria. Criteria expand to the exact inputs and outputs of that
 * check — the judge's raw answer, its reasoning, and any error — and
 * judge-based dimensions list the exact files the judge read.
 */
export function RewardBreakdownView({
  breakdown,
  rewards,
  className,
  selectedCriterion,
  onSelectCriterion,
}: RewardBreakdownViewProps) {
  // A deep-linked criterion scrolls into view once its row exists; one-shot
  // so later user toggles don't yank the scroll position.
  const scrolledToTargetRef = useRef(false);
  const hasBreakdown = breakdown !== null;
  useEffect(() => {
    if (scrolledToTargetRef.current || !selectedCriterion || !hasBreakdown) {
      return;
    }
    const row = document.querySelector(
      `[data-criterion="${CSS.escape(selectedCriterion)}"]`
    );
    if (row) {
      scrolledToTargetRef.current = true;
      row.scrollIntoView({ block: "center" });
    }
  }, [selectedCriterion, hasBreakdown]);

  if (!breakdown && !rewards) return null;
  const dimensionNames = new Set(
    breakdown?.dimensions.map((d) => d.name) ?? []
  );
  const aggregates = rewards
    ? splitRewardsMap(rewards, dimensionNames).aggregates
    : [];

  return (
    <div className={cn("space-y-2", className)}>
      {aggregates.length > 0 && (
        <div className="flex flex-wrap items-center gap-1.5">
          {aggregates.map(([name, value]) => (
            <span
              key={name}
              className="border-border flex items-center gap-1.5 rounded border px-2 py-0.5"
            >
              <span className="text-muted-foreground font-mono text-[10.5px]">
                {name}
              </span>
              <ScorePill value={value} />
            </span>
          ))}
        </div>
      )}
      {breakdown?.dimensions.map((dimension, index) => (
        <DimensionSection
          key={`${dimension.name}-${index}`}
          dimension={dimension}
          selectedCriterion={selectedCriterion}
          onSelectCriterion={onSelectCriterion}
        />
      ))}
      {breakdown?.truncated && (
        <p className="text-muted-foreground text-[10.5px]">
          Some reasoning was shortened to fit; the full report is in{" "}
          <span className="font-mono">
            {breakdown.detailsPath ?? "verifier/reward-details.json"}
          </span>
          .
        </p>
      )}
    </div>
  );
}
