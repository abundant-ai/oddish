import { cn } from "@/lib/utils";
import { AnalysisProse } from "@/components/analysis-prose";
import type { PreTrialFinding } from "@/lib/types";
import { TIER_BADGE, TIER_META, TIER_ORDER } from "./tokens";
import { FeedbackControl } from "./feedback-control";
import type { FeedbackRecord } from "./types";

function findingLocation(finding: PreTrialFinding): string | null {
  if (!finding.file) return null;
  const { line_start: start, line_end: end } = finding;
  if (!start) return finding.file;
  return `${finding.file}:${start}${end && end !== start ? `-${end}` : ""}`;
}

function ActionItemDetail({
  item,
  itemKey,
  onFeedback,
}: {
  item: PreTrialFinding;
  itemKey: string;
  onFeedback?: (r: FeedbackRecord) => void;
}) {
  const where = findingLocation(item);
  return (
    <div className="flex flex-col gap-1.5">
      {(item.dimension || item.problem_type || item.exploited) && (
        <div className="flex flex-wrap items-center gap-2">
          {(item.dimension || item.problem_type) && (
            <span className="text-muted-foreground font-mono text-[10px]">
              {[item.dimension, item.problem_type].filter(Boolean).join(" / ")}
            </span>
          )}
          {item.exploited ? (
            <span className="rounded border border-red-500/60 px-1.5 py-0.5 font-mono text-[9px] font-semibold tracking-wider text-red-500">
              EXPLOITED
            </span>
          ) : null}
        </div>
      )}

      {item.title ? (
        <h4 className="text-foreground text-sm leading-snug font-medium text-pretty">
          {item.title}
        </h4>
      ) : null}

      {where ? (
        <p className="text-muted-foreground font-mono text-[10.5px] break-all">
          {where}
        </p>
      ) : null}

      {item.detail ? (
        <AnalysisProse
          text={item.detail}
          className="text-foreground/90 mt-0.5"
        />
      ) : null}

      {item.recommendation ? (
        <div className="mt-0.5 flex items-baseline gap-2">
          <span className="text-muted-foreground shrink-0 font-mono text-[10px] tracking-widest">
            FIX
          </span>
          <AnalysisProse
            text={item.recommendation}
            className="text-foreground/90 min-w-0"
          />
        </div>
      ) : null}

      {onFeedback ? (
        <FeedbackControl
          label={`action item: ${item.title ?? itemKey}`}
          className="mt-1.5"
          onSubmit={(vote, note) =>
            onFeedback({
              target: { kind: "action_item", id: itemKey },
              vote,
              note,
            })
          }
        />
      ) : null}
    </div>
  );
}

/**
 * Action items grouped by severity tier, each tier its own independently
 * collapsible <details>, all starting collapsed.
 */
export function SeverityGroups({
  items,
  onFeedback,
  className,
  tierEffects,
}: {
  items: PreTrialFinding[];
  onFeedback?: (r: FeedbackRecord) => void;
  className?: string;
  /** Per-tier effect line; the default narrates trial classification. */
  tierEffects?: Partial<Record<string, string>>;
}) {
  const groups = TIER_ORDER.map((tier) => ({
    tier,
    meta: TIER_META[tier],
    items: items.filter((i) => (i.tier ?? "optional") === tier),
  })).filter((g) => g.items.length > 0);

  if (!groups.length) return null;

  return (
    <div className={cn("flex flex-col gap-2", className)}>
      {groups.map((group) => (
        <details
          key={group.tier}
          className="group border-border bg-background/40 rounded-lg border"
        >
          <summary className="hover:bg-foreground/5 flex cursor-pointer list-none flex-wrap items-center gap-2.5 px-3 py-2 transition-colors select-none">
            <span
              aria-hidden="true"
              className="text-muted-foreground text-[9px] transition-transform group-open:rotate-90"
            >
              &#9654;
            </span>
            <span
              className={cn(
                "rounded-md px-2 py-0.5 font-mono text-[9.5px] font-semibold tracking-wider",
                TIER_BADGE[group.tier],
              )}
            >
              {group.meta.label}
            </span>
            <span className="text-muted-foreground font-mono text-[10px]">
              {group.items.length} item{group.items.length === 1 ? "" : "s"}
            </span>
            <span className="text-muted-foreground min-w-0 flex-1 text-[11px] leading-relaxed text-pretty">
              {tierEffects?.[group.tier] ?? group.meta.labelEffect}
            </span>
          </summary>

          <ul className="divide-border border-border flex flex-col divide-y border-t">
            {group.items.map((item, index) => {
              const key = item.id ?? `${group.tier}-${item.title ?? index}`;
              return (
                <li key={key} className="px-3 py-3">
                  <ActionItemDetail
                    item={item}
                    itemKey={key}
                    onFeedback={onFeedback}
                  />
                </li>
              );
            })}
          </ul>
        </details>
      ))}
    </div>
  );
}
