"use client";

import { useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  Bot,
  ChevronDown,
  ChevronRight,
  FlaskConical,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { formatRewardValue, getRewardStyle } from "@/lib/status-config";
import type { Trial } from "@/lib/types";
import {
  aggregateScores,
  buildRewardDesign,
  embeddedRewardBreakdown,
  enrichDesignWithObserved,
  type RewardDesign,
  type RewardDesignCriterion,
  type RewardDesignDimension,
  type TaskFileContent,
} from "@/lib/reward-kit";

const TERMINAL_STATUSES = ["success", "failed", "cancelled", "skipped"];

/** Design files worth fetching; everything else in tests/ is irrelevant. */
function isDesignFile(path: string): boolean {
  if (!path.startsWith("tests/")) return false;
  return (
    path === "tests/test.sh" || path.endsWith(".toml") || path.endsWith(".py")
  );
}

const MAX_DESIGN_FILES = 24;

function aggregationLabel(aggregation: string): string {
  if (aggregation === "all_pass") return "∧ all_pass";
  if (aggregation === "any_pass") return "∨ any_pass";
  if (aggregation === "required_pass") return "∧ required_pass";
  if (aggregation === "threshold") return "≥ threshold";
  return "x̄ weighted_mean";
}

function aggregationSentence(aggregation: string, threshold: number): string {
  if (aggregation === "all_pass") {
    return "1.0 only when every entry scores above 0 — one failure zeroes it";
  }
  if (aggregation === "any_pass") return "1.0 when any entry scores above 0";
  if (aggregation === "required_pass") {
    return "1.0 only when every non-optional entry scores above 0";
  }
  if (aggregation === "threshold") {
    return `1.0 when the weighted mean reaches ${threshold}`;
  }
  return "weighted mean — partial credit flows through";
}

function ScoreChip({ value }: { value: number }) {
  const tone = value >= 1 ? "pass" : value <= 0 ? "fail" : "partial";
  return (
    <span
      className={cn(
        "shrink-0 rounded border px-1.5 py-px font-mono text-[11px] font-semibold tabular-nums",
        tone === "pass" &&
          "border-[color:var(--paper-pass)]/40 bg-[color:var(--paper-pass-bg)] text-[color:var(--paper-pass)]",
        tone === "fail" &&
          "border-[color:var(--paper-fail)]/40 bg-[color:var(--paper-fail-bg)] text-[color:var(--paper-fail)]"
      )}
      style={tone === "partial" ? getRewardStyle(value, "badge") : undefined}
    >
      {formatRewardValue(value)}
    </span>
  );
}

function CriterionDesignRow({
  criterion,
  whatIf,
  passing,
  onToggle,
}: {
  criterion: RewardDesignCriterion;
  whatIf: boolean;
  passing: boolean;
  onToggle: () => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const hasDetail = Boolean(criterion.description || criterion.source);

  return (
    <div className="border-t border-[color:var(--paper-line-2)] first:border-t-0">
      <div className="flex w-full items-center gap-2 px-3 py-1.5">
        <button
          type="button"
          onClick={() => hasDetail && setExpanded((v) => !v)}
          className={cn(
            "flex min-w-0 flex-1 items-center gap-2 text-left",
            hasDetail && "cursor-pointer"
          )}
          aria-expanded={hasDetail ? expanded : undefined}
        >
          {hasDetail ? (
            expanded ? (
              <ChevronDown className="h-3 w-3 shrink-0 text-[color:var(--paper-ink-4)]" />
            ) : (
              <ChevronRight className="h-3 w-3 shrink-0 text-[color:var(--paper-ink-4)]" />
            )
          ) : (
            <span className="w-3 shrink-0" />
          )}
          <span className="min-w-0 flex-1 truncate font-mono text-[11.5px] text-[color:var(--paper-ink)]">
            {criterion.name}
            {criterion.optional && (
              <span className="ml-1 text-[10px] text-[color:var(--paper-ink-3)]">
                (optional)
              </span>
            )}
          </span>
        </button>
        {criterion.type && (
          <span className="shrink-0 rounded border border-[color:var(--paper-line)] px-1.5 py-px font-mono text-[10px] text-[color:var(--paper-ink-3)]">
            {criterion.type}
          </span>
        )}
        <span className="shrink-0 font-mono text-[10px] text-[color:var(--paper-ink-3)] tabular-nums">
          w {criterion.weight}
        </span>
        {whatIf && (
          <button
            type="button"
            onClick={onToggle}
            className="flex shrink-0 overflow-hidden rounded border border-[color:var(--paper-line)] font-mono text-[10px]"
            aria-label={`Toggle ${criterion.name} pass/fail`}
          >
            <span
              className={cn(
                "px-1.5 py-px",
                passing
                  ? "bg-[color:var(--paper-pass-bg)] font-semibold text-[color:var(--paper-pass)]"
                  : "text-[color:var(--paper-ink-4)]"
              )}
            >
              pass
            </span>
            <span
              className={cn(
                "px-1.5 py-px",
                !passing
                  ? "bg-[color:var(--paper-fail-bg)] font-semibold text-[color:var(--paper-fail)]"
                  : "text-[color:var(--paper-ink-4)]"
              )}
            >
              fail
            </span>
          </button>
        )}
      </div>
      {expanded && hasDetail && (
        <div className="space-y-1 px-8 pb-2">
          {criterion.description && (
            <p className="text-[11.5px] text-[color:var(--paper-ink-2)]">
              {criterion.description}
            </p>
          )}
          <p className="font-mono text-[10px] text-[color:var(--paper-ink-4)]">
            {criterion.source}
          </p>
        </div>
      )}
    </div>
  );
}

function DimensionDesignCard({
  dimension,
  whatIf,
  states,
  score,
  onToggle,
  anchorId,
}: {
  dimension: RewardDesignDimension;
  whatIf: boolean;
  states: boolean[];
  score: number;
  onToggle: (index: number) => void;
  anchorId?: string;
}) {
  const judged = dimension.kind !== "programmatic";
  return (
    <div
      className="overflow-hidden rounded-[10px] border border-[color:var(--paper-line)] bg-[color:var(--paper-surface)]"
      id={anchorId}
    >
      <div className="flex items-center gap-2 bg-[color:var(--paper-surface-2)] px-3 py-1.5">
        <span className="min-w-0 flex-1 truncate font-mono text-[12px] font-semibold text-[color:var(--paper-ink)]">
          {dimension.name}
        </span>
        {judged ? (
          <span className="flex shrink-0 items-center gap-1 rounded border border-[color:var(--paper-running)]/40 bg-[color:var(--paper-running-bg)] px-1.5 py-px font-mono text-[10px] text-[color:var(--paper-running)]">
            <Bot className="h-3 w-3" />
            {dimension.kind === "agent" ? "agent judge" : "LLM judge"}
          </span>
        ) : (
          <span className="shrink-0 rounded border border-[color:var(--paper-line)] px-1.5 py-px font-mono text-[10px] text-[color:var(--paper-ink-3)]">
            programmatic
          </span>
        )}
        <span
          className="shrink-0 rounded border border-[color:var(--paper-line)] px-1.5 py-px font-mono text-[10px] font-semibold text-[color:var(--paper-ink-2)]"
          title={aggregationSentence(
            dimension.aggregation,
            dimension.threshold
          )}
        >
          {aggregationLabel(dimension.aggregation)}
        </span>
        <span className="shrink-0 font-mono text-[10px] text-[color:var(--paper-ink-3)] tabular-nums">
          w {dimension.weight}
        </span>
        {whatIf && <ScoreChip value={score} />}
      </div>
      {(dimension.judgeModel ||
        (dimension.judgeFiles?.length ?? 0) > 0 ||
        dimension.judgeTrajectory) && (
        <div className="flex flex-wrap items-center gap-1 border-t border-[color:var(--paper-line-2)] px-3 py-1">
          {dimension.judgeModel && (
            <span className="font-mono text-[10px] text-[color:var(--paper-ink-2)]">
              {dimension.judgeModel}
            </span>
          )}
          {(dimension.judgeFiles ?? []).map((file) => (
            <span
              key={file}
              className="rounded bg-[color:var(--paper-bg-2)] px-1.5 py-px font-mono text-[10px] text-[color:var(--paper-ink-3)]"
            >
              reads {file}
            </span>
          ))}
          {dimension.judgeTrajectory && (
            <span className="rounded bg-[color:var(--paper-bg-2)] px-1.5 py-px font-mono text-[10px] text-[color:var(--paper-ink-3)]">
              reads trajectory
            </span>
          )}
        </div>
      )}
      {dimension.criteria.map((criterion, index) => (
        <CriterionDesignRow
          key={`${criterion.name}-${index}`}
          criterion={criterion}
          whatIf={whatIf}
          passing={states[index] ?? true}
          onToggle={() => onToggle(index)}
        />
      ))}
      {!dimension.criteriaComplete && (
        <div className="flex items-center gap-1.5 border-t border-[color:var(--paper-line-2)] px-3 py-1 text-[10.5px] text-[color:var(--paper-ink-3)]">
          <AlertTriangle className="h-3 w-3" />
          Criteria are registered dynamically in {dimension.source}; this list
          is what a completed trial reported.
        </div>
      )}
    </div>
  );
}

export interface RewardDesignCardProps {
  taskId?: string;
  taskVersion?: number;
  trials: Trial[];
  apiBaseUrl?: string;
  /**
   * Files endpoint override (e.g. the task files pane's `filesUrl`). Defaults
   * to the task files API derived from `taskId`. Content responses may be
   * either the task API's `{content}` JSON or raw text; both are handled.
   */
  filesUrl?: string;
  /** Rendered when loading finished and no reward design was found. */
  emptyState?: React.ReactNode;
  /**
   * Hash-addressable anchor: the section gets this id and each dimension
   * gets `<anchorId>-<dimension>`, so `#reward-design` /
   * `#reward-design-llmj` deep-link straight to the design. Omitted in
   * drawer contexts to keep page ids unique.
   */
  anchorId?: string;
}

/**
 * The task's reward program, reconstructed from its RewardKit tests and
 * rendered as an explorable design: criteria grouped into dimensions, judge
 * configuration (model + exact input files), and the reward.toml aggregates
 * with their aggregation semantics. What-if mode simulates outcomes by
 * toggling criteria pass/fail and recomputing scores through the same
 * aggregation rules RewardKit applies.
 *
 * Renders nothing for tasks that are not RewardKit-based.
 */
export function RewardDesignCard({
  taskId,
  taskVersion,
  trials,
  apiBaseUrl = "/api",
  filesUrl,
  emptyState = null,
  anchorId,
}: RewardDesignCardProps) {
  const [design, setDesign] = useState<RewardDesign | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [whatIf, setWhatIf] = useState(false);
  // criterion pass/fail per dimension for what-if, keyed by dimension name.
  const [states, setStates] = useState<Record<string, boolean[]>>({});

  // The newest completed trial's breakdown fills in criteria a static scan
  // cannot see (python check files register criteria at runtime).
  const observedTrialId = useMemo(() => {
    const candidates = trials
      .filter(
        (t) =>
          !t.is_probe &&
          t.reward !== null &&
          TERMINAL_STATUSES.includes(t.status)
      )
      .sort((a, b) => (b.finished_at ?? "").localeCompare(a.finished_at ?? ""));
    return candidates[0]?.id ?? null;
  }, [trials]);

  useEffect(() => {
    const controller = new AbortController();
    setDesign(null);
    setLoaded(false);

    const filesBase =
      filesUrl?.replace(/\/$/, "") ??
      (taskId
        ? `${apiBaseUrl.replace(/\/$/, "")}/tasks/${encodeURIComponent(taskId)}/files`
        : null);
    if (!filesBase) {
      setLoaded(true);
      return;
    }

    async function load(base: string) {
      try {
        const params = new URLSearchParams();
        params.set("recursive", "1");
        // Metadata-only listing, matching the task files contract (and the
        // e2e assertion on it): file bodies come from the per-file fetches
        // below, never inlined into the tree response.
        params.set("inline", "0");
        params.set("presign", "0");
        if (taskVersion != null) params.set("version", String(taskVersion));
        const listingResponse = await fetch(`${base}?${params}`, {
          signal: controller.signal,
        });
        if (!listingResponse.ok) return;
        const listing = (await listingResponse.json()) as {
          files?: { path?: string; content?: string }[];
        };
        const candidates = (listing.files ?? [])
          .filter(
            (f): f is { path: string; content?: string } =>
              typeof f.path === "string" && isDesignFile(f.path)
          )
          .slice(0, MAX_DESIGN_FILES);
        if (candidates.length === 0) return;

        const contents: TaskFileContent[] = await Promise.all(
          candidates.map(async (file) => {
            if (typeof file.content === "string") {
              return { path: file.path, content: file.content };
            }
            const contentParams = new URLSearchParams();
            if (taskVersion != null) {
              contentParams.set("version", String(taskVersion));
            }
            const response = await fetch(
              `${base}/${encodeURIComponent(file.path)}${
                contentParams.size > 0 ? `?${contentParams}` : ""
              }`,
              { signal: controller.signal }
            );
            if (!response.ok) return { path: file.path, content: "" };
            // Task files endpoints answer `{content}` JSON; filesUrl-driven
            // panes may answer raw text. Probe rather than assume.
            const body = await response.text();
            try {
              const data = JSON.parse(body) as { content?: string };
              if (data && typeof data.content === "string") {
                return { path: file.path, content: data.content };
              }
            } catch {
              // Raw text response.
            }
            return { path: file.path, content: body };
          })
        );

        let built = buildRewardDesign(contents);
        if (built === null) return;

        if (observedTrialId) {
          try {
            const trialResponse = await fetch(
              `${apiBaseUrl.replace(/\/$/, "")}/trials/${encodeURIComponent(observedTrialId)}`,
              { signal: controller.signal }
            );
            if (trialResponse.ok) {
              const trial = (await trialResponse.json()) as {
                result?: Record<string, unknown> | null;
              };
              const observed = embeddedRewardBreakdown(trial.result);
              if (observed) built = enrichDesignWithObserved(built, observed);
            }
          } catch {
            // Optional enrichment only.
          }
        }

        if (!controller.signal.aborted) setDesign(built);
      } catch {
        // A task without readable files simply shows no design card.
      }
    }

    void load(filesBase).finally(() => {
      if (!controller.signal.aborted) setLoaded(true);
    });
    return () => controller.abort();
  }, [apiBaseUrl, filesUrl, taskId, taskVersion, observedTrialId]);

  // Keyed by array index: mixed tests directories produce same-named
  // dimensions (judge + programmatic), which must keep independent state.
  const dimensionScores = useMemo(() => {
    const scores: Record<string, number> = {};
    (design?.dimensions ?? []).forEach((dimension, index) => {
      const dimensionStates = states[String(index)] ?? [];
      scores[String(index)] = aggregateScores(
        dimension.criteria.map((criterion, index) => ({
          value: (dimensionStates[index] ?? true) ? 1 : 0,
          weight: criterion.weight,
          optional: criterion.optional,
        })),
        dimension.aggregation,
        dimension.threshold
      );
    });
    return scores;
  }, [design, states]);

  // A hash address scrolls to its section once the async design exists —
  // the browser's native hash jump fires before this section renders.
  useEffect(() => {
    if (!design || !anchorId) return;
    const hash = window.location.hash.slice(1);
    if (!hash || (hash !== anchorId && !hash.startsWith(`${anchorId}-`))) {
      return;
    }
    document.getElementById(hash)?.scrollIntoView({ block: "start" });
  }, [design, anchorId]);

  if (!design) return loaded ? <>{emptyState}</> : null;

  const criteriaCount = design.dimensions.reduce(
    (sum, d) => sum + d.criteria.length,
    0
  );

  // RewardKit first collapses same-named groups into one named score
  // (weighted by reward_weight), then feeds the named scores into the
  // reward.toml aggregation with each name's summed weight. Aggregating the
  // groups directly would let one failing group zero an all_pass that the
  // collapsed name would survive.
  const collapsedByName = new Map<string, { sum: number; weight: number }>();
  design.dimensions.forEach((dimension, index) => {
    const score = dimensionScores[String(index)] ?? 0;
    const weight = dimension.weight || 1;
    const entry = collapsedByName.get(dimension.name) ?? {
      sum: 0,
      weight: 0,
    };
    entry.sum += score * weight;
    entry.weight += weight;
    collapsedByName.set(dimension.name, entry);
  });
  const dimensionEntries = [...collapsedByName.values()].map((entry) => ({
    value: entry.weight === 0 ? 0 : entry.sum / entry.weight,
    weight: entry.weight,
  }));

  return (
    <div className="space-y-3" id={anchorId}>
      <div className="flex items-baseline justify-between gap-2">
        <h2 className="font-mono text-[12px] font-semibold tracking-[0.06em] text-[color:var(--paper-ink-2)] uppercase">
          Reward Design
        </h2>
        <div className="flex items-center gap-3">
          <span className="font-mono text-[10.5px] text-[color:var(--paper-ink-3)]">
            RewardKit · {criteriaCount} criteri
            {criteriaCount === 1 ? "on" : "a"} · {design.dimensions.length}{" "}
            dimension{design.dimensions.length === 1 ? "" : "s"}
            {design.aggregates.length > 0 &&
              ` · ${design.aggregates.length} reward${
                design.aggregates.length === 1 ? "" : "s"
              }`}
          </span>
          <button
            type="button"
            onClick={() => setWhatIf((v) => !v)}
            className={cn(
              "flex items-center gap-1 rounded border px-2 py-0.5 font-mono text-[10.5px]",
              whatIf
                ? "border-[color:var(--paper-ink-3)] bg-[color:var(--paper-surface-2)] font-semibold text-[color:var(--paper-ink)]"
                : "border-[color:var(--paper-line)] text-[color:var(--paper-ink-3)] hover:text-[color:var(--paper-ink-2)]"
            )}
            aria-pressed={whatIf}
          >
            <FlaskConical className="h-3 w-3" />
            What-if
          </button>
        </div>
      </div>

      {whatIf && (
        <p className="text-[11px] text-[color:var(--paper-ink-3)]">
          Toggle criteria pass/fail to see how scores flow through each
          dimension&apos;s aggregation into the final rewards.
        </p>
      )}

      <div className="grid gap-3 lg:grid-cols-2">
        {design.dimensions.map((dimension, dimIndex) => (
          <DimensionDesignCard
            key={`${dimension.name}-${dimIndex}`}
            dimension={dimension}
            // The first occurrence of a name owns the clean anchor; later
            // same-named groups (mixed judge + programmatic directories) get
            // an index suffix so page ids stay unique.
            anchorId={
              anchorId
                ? design.dimensions.findIndex(
                    (d) => d.name === dimension.name
                  ) === dimIndex
                  ? `${anchorId}-${dimension.name}`
                  : `${anchorId}-${dimension.name}-${dimIndex}`
                : undefined
            }
            whatIf={whatIf}
            states={states[String(dimIndex)] ?? []}
            score={dimensionScores[String(dimIndex)] ?? 0}
            onToggle={(index) =>
              setStates((previous) => {
                const next = [
                  ...(previous[String(dimIndex)] ??
                    dimension.criteria.map(() => true)),
                ];
                next[index] = !(next[index] ?? true);
                return { ...previous, [String(dimIndex)]: next };
              })
            }
          />
        ))}
      </div>

      {design.aggregates.length > 0 && (
        <div className="flex flex-wrap gap-2">
          {design.aggregates.map((aggregate) => (
            <div
              key={aggregate.name}
              className="flex items-center gap-2 rounded-[10px] border border-[color:var(--paper-line)] bg-[color:var(--paper-surface)] px-3 py-1.5"
              title={aggregationSentence(
                aggregate.aggregation,
                aggregate.threshold
              )}
            >
              <span className="font-mono text-[12px] font-semibold text-[color:var(--paper-ink)]">
                {aggregate.name}
              </span>
              <span className="rounded border border-[color:var(--paper-line)] px-1.5 py-px font-mono text-[10px] font-semibold text-[color:var(--paper-ink-2)]">
                {aggregationLabel(aggregate.aggregation)}
              </span>
              <span className="font-mono text-[10px] text-[color:var(--paper-ink-4)]">
                {design.dimensions.map((d) => d.name).join(" · ")}
              </span>
              {whatIf && (
                <ScoreChip
                  value={aggregateScores(
                    dimensionEntries,
                    aggregate.aggregation,
                    aggregate.threshold
                  )}
                />
              )}
              {aggregate.name === "reward" && (
                <span className="font-mono text-[10px] text-[color:var(--paper-ink-4)]">
                  headline
                </span>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
