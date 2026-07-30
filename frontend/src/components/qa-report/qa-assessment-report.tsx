import { cn } from "@/lib/utils";
import { AnalysisProse } from "@/components/analysis-prose";
import type { PreTrialFinding } from "@/lib/types";
import { FALLBACK_TOKEN, VERDICT_TOKENS } from "./tokens";
import { CopyJsonButton } from "./copy-json-button";
import { FeedbackControl } from "./feedback-control";
import { SeverityGroups } from "./action-items";
import type { FeedbackRecord } from "./types";

const isNa = (s: string | null | undefined) => !s || /^n\/a/i.test(s);

export function QaAssessmentReport({
  classification,
  subtype,
  rootCause,
  recommendation,
  evidence,
  actionItems,
  log,
  duration,
  raw,
  onFeedback,
  className,
}: {
  classification: string;
  subtype?: string | null;
  rootCause?: string | null;
  recommendation?: string | null;
  evidence?: string | null;
  actionItems?: PreTrialFinding[] | null;
  /** the analyzer's run log, shown as a collapsed fold under Evidence */
  log?: string | null;
  duration?: string | null;
  /** the raw analysis object, for the copy button */
  raw?: unknown;
  /**
   * Vote controls render only when this is provided — a vote that persists
   * nowhere is worse than no vote control.
   */
  onFeedback?: (record: FeedbackRecord) => void;
  className?: string;
}) {
  const token = VERDICT_TOKENS[classification] ?? FALLBACK_TOKEN;
  const Icon = token.icon;
  const items = actionItems ?? [];
  const mustFix = items.filter((i) => i.tier === "must_fix").length;

  return (
    <article
      className={cn("overflow-hidden rounded-xl border", token.card, className)}
    >
      <header className="border-border/60 flex flex-wrap items-center gap-2.5 border-b px-3 py-2.5">
        <Icon
          aria-hidden="true"
          className={cn("size-4.5 shrink-0", token.accent)}
          strokeWidth={2}
        />
        <h2
          className={cn(
            "font-mono text-sm font-semibold tracking-wide",
            token.accent,
          )}
        >
          {classification.replace(/_/g, " ")}
        </h2>
        {subtype && !isNa(subtype) ? (
          <span
            className={cn(
              "rounded-md border px-2 py-0.5 font-mono text-[10px]",
              token.chip,
              token.accent,
            )}
          >
            {subtype.replace(/_/g, " ")}
          </span>
        ) : null}

        <div className="text-muted-foreground ml-auto flex shrink-0 items-center gap-2 font-mono text-[10px]">
          {mustFix > 0 ? (
            <span className="bg-destructive/15 text-destructive rounded-md px-1.5 py-0.5 font-semibold">
              {mustFix} must_fix
            </span>
          ) : null}
          {duration ? <span>{duration}</span> : null}
          {raw !== undefined ? (
            <CopyJsonButton value={raw} label="the full assessment" />
          ) : null}
        </div>
      </header>

      <div className="px-3 py-3">
        {rootCause ? (
          <AnalysisProse text={rootCause} className="text-foreground/90" />
        ) : (
          <p className="text-muted-foreground text-xs">
            The analysis produced no root cause.
          </p>
        )}

        {recommendation && !isNa(recommendation) ? (
          <div className="mt-2 flex items-baseline gap-2">
            <span className="text-muted-foreground shrink-0 font-mono text-[10px] tracking-widest">
              FIX
            </span>
            <AnalysisProse
              text={recommendation}
              className="text-foreground/90 min-w-0"
            />
          </div>
        ) : null}

        {onFeedback ? (
          <FeedbackControl
            label={`the ${classification.replace(/_/g, " ")} verdict`}
            className="mt-3"
            onSubmit={(vote, note) =>
              onFeedback({
                target: { kind: "verdict", classification },
                vote,
                note,
              })
            }
          />
        ) : null}

        {items.length > 0 ? (
          <section className="mt-4">
            <h3 className="text-muted-foreground font-mono text-[10px] font-semibold tracking-widest uppercase">
              Action items ({items.length})
            </h3>
            <SeverityGroups
              items={items}
              onFeedback={onFeedback}
              className="mt-2"
            />
          </section>
        ) : null}

        {evidence ? (
          <details className="group mt-3">
            <summary className="text-muted-foreground hover:text-foreground flex cursor-pointer list-none items-center gap-2 text-[11px] font-medium transition-colors select-none">
              <span
                aria-hidden="true"
                className="text-[9px] transition-transform group-open:rotate-90"
              >
                &#9654;
              </span>
              Evidence
              <CopyJsonButton
                value={evidence}
                label="the evidence"
                compact
                className="ml-auto"
              />
            </summary>
            <div className="border-border mt-2 border-l pl-3">
              <AnalysisProse
                text={evidence}
                className="text-muted-foreground/90"
              />
            </div>
          </details>
        ) : null}

        {log ? (
          <details className="group mt-3">
            <summary className="text-muted-foreground hover:text-foreground flex cursor-pointer list-none items-center gap-2 text-[11px] font-medium transition-colors select-none">
              <span
                aria-hidden="true"
                className="text-[9px] transition-transform group-open:rotate-90"
              >
                &#9654;
              </span>
              Analysis log
            </summary>
            <pre className="bg-muted/40 mt-2 max-h-48 overflow-auto rounded p-2 font-mono text-[10.5px] leading-relaxed whitespace-pre-wrap">
              {log}
            </pre>
          </details>
        ) : null}
      </div>
    </article>
  );
}
