import { useEffect, useRef, type ReactNode } from "react";

import { cn } from "@/lib/utils";
import { AnalysisProse } from "@/components/analysis-prose";
import type { PreTrialFinding } from "@/lib/types";
import { TIER_BADGE, TIER_LABELS, TIER_ORDER } from "./tokens";
import { CopyJsonButton } from "./copy-json-button";
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
  renderItemFooter,
  findingLink,
}: {
  findingLink?: (item: PreTrialFinding, file?: boolean) => string;
  item: PreTrialFinding;
  itemKey: string;
  onFeedback?: (record: FeedbackRecord) => Promise<void>;
  renderItemFooter?: (item: PreTrialFinding, itemKey: string) => ReactNode;
}) {
  const where = findingLocation(item);
  return (
    <div className="flex flex-col gap-1.5">
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
        <CopyJsonButton
          value={item}
          label={`action item: ${item.title ?? itemKey}`}
          compact
          className="ml-auto"
        />
      </div>

      {where ? (
        <p className="text-muted-foreground font-mono text-[10.5px] break-all">
          {findingLink ? (
            <a className="underline" href={findingLink(item, true)}>
              Open {where}
            </a>
          ) : (
            where
          )}
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

      {renderItemFooter?.(item, itemKey)}

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

/** Each finding opens independently; addressed findings open from the URL. */
export function FindingList({
  items,
  onFeedback,
  className,
  renderItemFooter,
  selectedFinding,
  findingLink,
}: {
  selectedFinding?: string | null;
  findingLink?: (item: PreTrialFinding, file?: boolean) => string;
  items: PreTrialFinding[];
  onFeedback?: (record: FeedbackRecord) => Promise<void>;
  className?: string;
  /** Extra content under an item — e.g. links to the trials that surfaced it. */
  renderItemFooter?: (item: PreTrialFinding, itemKey: string) => ReactNode;
}) {
  const root = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!selectedFinding) return;
    const matches = Array.from(
      root.current?.querySelectorAll<HTMLElement>("[data-finding]") ?? []
    ).filter(
      (node) =>
        node.dataset.finding === selectedFinding ||
        node.dataset.findingLink === selectedFinding
    );
    for (const item of matches) {
      const disclosure = item.closest("details");
      if (disclosure) disclosure.open = true;
    }
    matches[0]?.scrollIntoView({ block: "center" });
  }, [selectedFinding, items]);
  const ordered = TIER_ORDER.flatMap((tier) =>
    items.filter((item) => (item.tier ?? "optional") === tier)
  );
  if (!ordered.length) return null;

  return (
    <div ref={root} className={cn("flex flex-col gap-2", className)}>
      {ordered.map((item, index) => {
        const tier = item.tier ?? "optional";
        const key = item.id ?? `${tier}-${item.title ?? index}`;
        return (
          <details
            key={key}
            data-finding={item.id}
            data-finding-link={item.links_to}
            className={cn(
              "group border-border bg-background/40 rounded-lg border",
              item.id === selectedFinding && "ring-1 ring-amber-500/40"
            )}
          >
            <summary className="hover:bg-foreground/5 flex cursor-pointer list-none items-start gap-3 px-4 py-3 select-none">
              <span
                aria-hidden="true"
                className="text-muted-foreground mt-1 text-[9px] transition-transform group-open:rotate-90"
              >
                &#9654;
              </span>
              <span
                className={cn(
                  "shrink-0 rounded px-2 py-0.5 text-xs font-medium",
                  TIER_BADGE[tier]
                )}
              >
                {TIER_LABELS[tier]}
              </span>
              <h4 className="min-w-0 text-sm leading-relaxed font-medium">
                {item.title || `Finding ${index + 1}`}
              </h4>
            </summary>
            <div className="border-border border-t px-4 py-4">
              <ActionItemDetail
                item={item}
                findingLink={findingLink}
                itemKey={key}
                onFeedback={onFeedback}
                renderItemFooter={renderItemFooter}
              />
            </div>
          </details>
        );
      })}
    </div>
  );
}
