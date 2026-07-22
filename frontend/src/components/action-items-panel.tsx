"use client";

import { ActionItem, DIMENSION_META, TIER_META, groupByDimension } from "@/lib/action-items";

interface ActionItemsPanelProps {
  items: ActionItem[];
  onOpenFile: (file: string, lineStart: number | null, lineEnd?: number | null) => void;
}

export function ActionItemsPanel({ items, onOpenFile }: ActionItemsPanelProps) {
  if (!items.length) {
    return (
      <div className="rounded border bg-muted/20 p-3 text-sm text-emerald-700">
        No QA action items — task held up.
      </div>
    );
  }
  const groups = groupByDimension(items);
  const dimensions = Object.keys(groups) as (keyof typeof groups)[];

  return (
    <div className="space-y-3 rounded border-2 border-primary/30 bg-primary/5 p-4">
      <p className="text-xs font-semibold uppercase tracking-wide text-foreground">QA action items</p>
      {dimensions.map((dim) =>
        groups[dim].length ? (
          <div key={dim} className="rounded border bg-muted/20 p-3">
            <p className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
              {DIMENSION_META[dim].label}
            </p>
            <ul className="space-y-2">
              {groups[dim].map((item) => {
                const meta = TIER_META[item.tier] ?? TIER_META.should_fix;
                return (
                  <li
                    key={item.id}
                    className={`flex items-start gap-2 rounded text-sm ${
                      item.exploited
                        ? "border border-red-500/40 bg-red-500/5 p-2 dark:border-red-400/40 dark:bg-red-400/10"
                        : ""
                    }`}
                  >
                    <span className={`mt-0.5 shrink-0 rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide ${meta.cls}`}>
                      {meta.label}
                    </span>
                    <span className="leading-snug">
                      <span className="font-medium">{item.title}</span>
                      {item.exploited ? (
                        <span className="ml-2 rounded bg-red-500/15 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-red-600">
                          exploited
                        </span>
                      ) : null}
                      <span className="text-muted-foreground"> — {item.detail}</span>
                      <div className="mt-0.5 text-xs">
                        <button
                          type="button"
                          className="font-mono text-primary underline underline-offset-2"
                          onClick={() =>
                            onOpenFile(item.file, item.line_start, item.line_end)
                          }
                        >
                          {item.file}:{item.line_start}
                          {item.line_end !== item.line_start ? `-${item.line_end}` : ""}
                        </button>
                        <span className="text-muted-foreground"> · {item.recommendation}</span>
                      </div>
                      {item.exploited && item.exploit_evidence ? (
                        <div className="mt-0.5 text-xs text-red-600/90">Exploited: {item.exploit_evidence}</div>
                      ) : null}
                    </span>
                  </li>
                );
              })}
            </ul>
          </div>
        ) : null,
      )}
    </div>
  );
}
