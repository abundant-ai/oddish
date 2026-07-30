"use client";

/**
 * QA assessment report card: a verdict header over the root cause, action
 * items grouped by severity tier, then evidence. Expanded by default — the
 * reviewer landed here to read this trial, so nothing at the top level starts
 * hidden. The only collapsed regions are the severity groups and evidence.
 *
 * Colour carries good/bad, not success/failure: green = valid signal,
 * amber = task needs fixing, red = false positive, orange = infrastructure
 * broke. Severity is deliberately NOT hue-coded like verdicts are — only
 * must_fix gets colour, because only must_fix can change the label; the
 * other tiers step down in weight instead, so a severity badge can never be
 * mistaken for a verdict.
 */

import { useEffect, useId, useRef, useState } from "react";
import {
  Check,
  CircleCheck,
  Copy,
  ShieldAlert,
  ThumbsDown,
  ThumbsUp,
  TriangleAlert,
  Unplug,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { AnalysisProse } from "@/components/analysis-prose";
import type { PreTrialFinding } from "@/lib/types";

/* ------------------------------------------------------------------ types */

export type FeedbackVote = "agree" | "disagree";

/** Emitted whenever a reviewer votes. `target` says what was voted on. */
export type FeedbackRecord = {
  target:
    | { kind: "verdict"; classification: string }
    | { kind: "action_item"; id: string };
  vote: FeedbackVote;
  note?: string;
};

export type QaAssessmentReportProps = {
  /** e.g. "GOOD_FAILURE" — rendered with the underscore replaced. */
  classification: string;
  /** short failure-mode chip, e.g. "premature_stop"; skipped when N/A */
  subtype?: string | null;
  rootCause?: string | null;
  /** top-level fix from the classifier; skipped when N/A */
  recommendation?: string | null;
  evidence?: string | null;
  actionItems?: PreTrialFinding[] | null;
  /** e.g. "42s" — how long the analysis ran */
  duration?: string | null;
  /** the raw analysis object, for the copy button */
  raw?: unknown;
  /**
   * Fires on every agree/disagree, on the verdict and on each action item.
   * The vote controls render only when this is provided — a vote that
   * persists nowhere is worse than no vote control.
   */
  onFeedback?: (record: FeedbackRecord) => void;
  className?: string;
};

/* --------------------------------------------------------------- metadata */

/** Highest first. Drives group order. */
const TIER_ORDER = ["must_fix", "should_fix", "optional"] as const;

const TIER_META: Record<string, { label: string; labelEffect: string }> = {
  must_fix: {
    label: "MUST FIX",
    labelEffect:
      "Blocks GOOD FAILURE — a failed run with a must_fix item is BAD FAILURE.",
  },
  should_fix: {
    label: "SHOULD FIX",
    labelEffect: "Does not change the label.",
  },
  optional: {
    label: "OPTIONAL",
    labelEffect: "Does not change the label.",
  },
};

const TIER_BADGE: Record<string, string> = {
  must_fix: "bg-destructive text-destructive-foreground",
  should_fix: "border-foreground/25 bg-foreground/10 text-foreground border",
  optional: "border-border text-muted-foreground border bg-transparent",
};

type VerdictToken = {
  icon: typeof CircleCheck;
  accent: string;
  card: string;
  chip: string;
};

const VERDICT_TOKENS: Record<string, VerdictToken> = {
  GOOD_SUCCESS: {
    icon: CircleCheck,
    accent: "text-emerald-600 dark:text-emerald-400",
    card: "border-emerald-500/30 bg-emerald-500/5",
    chip: "border-emerald-500/40",
  },
  GOOD_FAILURE: {
    icon: CircleCheck,
    accent: "text-emerald-600 dark:text-emerald-400",
    card: "border-emerald-500/30 bg-emerald-500/5",
    chip: "border-emerald-500/40",
  },
  BAD_FAILURE: {
    icon: TriangleAlert,
    accent: "text-amber-600 dark:text-amber-400",
    card: "border-amber-500/30 bg-amber-500/5",
    chip: "border-amber-500/40",
  },
  BAD_SUCCESS: {
    icon: ShieldAlert,
    accent: "text-red-600 dark:text-red-400",
    card: "border-red-500/35 bg-red-500/5",
    chip: "border-red-500/45",
  },
  HARNESS_ERROR: {
    icon: Unplug,
    accent: "text-orange-600 dark:text-orange-400",
    card: "border-orange-500/30 bg-orange-500/5",
    chip: "border-orange-500/40",
  },
};

const FALLBACK_TOKEN: VerdictToken = {
  icon: TriangleAlert,
  accent: "text-muted-foreground",
  card: "border-border bg-muted/20",
  chip: "border-border",
};

const isNa = (s: string | null | undefined) => !s || /^n\/a/i.test(s);

function findingLocation(finding: PreTrialFinding): string | null {
  if (!finding.file) return null;
  const { line_start: start, line_end: end } = finding;
  if (!start) return finding.file;
  return `${finding.file}:${start}${end && end !== start ? `-${end}` : ""}`;
}

/* ------------------------------------------------------------ copy button */

/**
 * Copies a value to the clipboard as formatted JSON. Deliberately used at
 * only two levels — the whole assessment, and the evidence — because every
 * other level is a subset of the assessment JSON.
 */
function CopyJsonButton({
  value,
  label,
  compact = false,
  className,
}: {
  value: unknown;
  /** describes what gets copied, for the accessible name */
  label: string;
  compact?: boolean;
  className?: string;
}) {
  const [copied, setCopied] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    return () => {
      if (timer.current) clearTimeout(timer.current);
    };
  }, []);

  async function copy(event: React.MouseEvent) {
    // The compact variant sits inside a <summary>; a plain click there also
    // toggles the disclosure the button lives in.
    event.preventDefault();
    event.stopPropagation();
    const json = JSON.stringify(value, null, 2);
    try {
      await navigator.clipboard.writeText(json);
    } catch {
      // Clipboard API is unavailable in some embedded/insecure contexts.
      const ta = document.createElement("textarea");
      ta.value = json;
      ta.setAttribute("readonly", "");
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      document.body.removeChild(ta);
    }
    setCopied(true);
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => setCopied(false), 1600);
  }

  const Icon = copied ? Check : Copy;

  return (
    <button
      type="button"
      onClick={copy}
      aria-label={copied ? `${label} copied to clipboard` : `Copy ${label} as JSON`}
      className={cn(
        "focus-visible:ring-ring inline-flex shrink-0 items-center gap-1.5 rounded-md border font-mono text-[10px] transition-colors focus-visible:ring-2 focus-visible:outline-none",
        compact ? "px-1.5 py-1" : "px-2 py-1",
        copied
          ? "border-emerald-500/60 bg-emerald-500/10 text-emerald-600 dark:text-emerald-400"
          : "border-border text-muted-foreground hover:bg-secondary hover:text-foreground",
        className,
      )}
    >
      <Icon aria-hidden="true" className="size-3" strokeWidth={2} />
      {compact ? null : <span>{copied ? "COPIED" : "COPY JSON"}</span>}
    </button>
  );
}

/* --------------------------------------------------------------- feedback */

/**
 * Agree / disagree control for one reviewable claim. Disagreeing opens an
 * optional free-text note, because a bare downvote is not actionable.
 */
function FeedbackControl({
  label,
  onSubmit,
  className,
}: {
  /** describes what is being voted on, for screen readers */
  label: string;
  onSubmit: (vote: FeedbackVote, note?: string) => void;
  className?: string;
}) {
  const [vote, setVote] = useState<FeedbackVote | null>(null);
  const [note, setNote] = useState("");
  const [submittedNote, setSubmittedNote] = useState<string | null>(null);
  const noteId = useId();

  const base =
    "focus-visible:ring-ring inline-flex items-center gap-1.5 rounded-md border px-2 py-1 font-mono text-[10px] tracking-wide transition-colors focus-visible:ring-2 focus-visible:outline-none";
  const idle =
    "border-border text-muted-foreground hover:bg-secondary hover:text-foreground";

  function pick(next: FeedbackVote) {
    const same = vote === next;
    setVote(same ? null : next);
    if (same || next === "agree") {
      setNote("");
      setSubmittedNote(null);
    }
    if (!same && next === "agree") onSubmit("agree");
  }

  return (
    <div className={cn("flex flex-col gap-2", className)}>
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-muted-foreground mr-1 font-mono text-[10px] tracking-wider">
          {vote === null
            ? "IS THIS RIGHT?"
            : vote === "agree"
              ? "MARKED AGREE"
              : "MARKED DISAGREE"}
        </span>
        <button
          type="button"
          aria-pressed={vote === "agree"}
          onClick={() => pick("agree")}
          className={cn(
            base,
            vote === "agree"
              ? "border-emerald-500/60 bg-emerald-500/10 text-emerald-600 dark:text-emerald-400"
              : idle,
          )}
        >
          <ThumbsUp aria-hidden="true" className="size-3" />
          Agree
          <span className="sr-only">with {label}</span>
        </button>
        <button
          type="button"
          aria-pressed={vote === "disagree"}
          onClick={() => pick("disagree")}
          className={cn(
            base,
            vote === "disagree"
              ? "border-destructive/60 bg-destructive/15 text-destructive"
              : idle,
          )}
        >
          <ThumbsDown aria-hidden="true" className="size-3" />
          Disagree
          <span className="sr-only">with {label}</span>
        </button>
      </div>

      {vote === "disagree" && submittedNote === null ? (
        <div className="flex flex-col gap-2">
          <label
            htmlFor={noteId}
            className="text-muted-foreground font-mono text-[10px] tracking-wider"
          >
            WHY? (NOT REQUIRED)
          </label>
          <textarea
            id={noteId}
            value={note}
            onChange={(e) => setNote(e.target.value)}
            rows={2}
            placeholder="e.g. the agent had no way to know the budget remaining"
            className="border-input bg-background text-foreground placeholder:text-muted-foreground focus-visible:ring-ring w-full resize-y rounded-md border px-2.5 py-1.5 text-xs leading-relaxed focus-visible:ring-2 focus-visible:outline-none"
          />
          <div>
            <button
              type="button"
              onClick={() => {
                setSubmittedNote(note.trim());
                onSubmit("disagree", note.trim() || undefined);
              }}
              className="bg-primary text-primary-foreground focus-visible:ring-ring rounded-md px-2.5 py-1 font-mono text-[10px] tracking-wide transition-opacity hover:opacity-90 focus-visible:ring-2 focus-visible:outline-none"
            >
              Submit
            </button>
          </div>
        </div>
      ) : null}

      {submittedNote !== null ? (
        <p className="text-muted-foreground text-xs leading-relaxed">
          {submittedNote ? (
            <>
              <span className="font-mono text-[10px] tracking-wider">
                YOUR NOTE{" "}
              </span>
              {submittedNote}
            </>
          ) : (
            "Disagreement recorded."
          )}
        </p>
      ) : null}
    </div>
  );
}

/* ----------------------------------------------------------- action items */

/**
 * One action item, rendered inline inside its expanded severity group — no
 * surface, border, or padding of its own, so it reads as the group's content
 * rather than a card nested in a card. The group header already states the
 * severity tier, so it is not repeated here.
 */
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
      {(item.dimension || item.problem_type) && (
        <span className="text-muted-foreground font-mono text-[10px]">
          {[item.dimension, item.problem_type].filter(Boolean).join(" / ")}
        </span>
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
        <AnalysisProse text={item.detail} className="text-foreground/90 mt-0.5" />
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
            onFeedback({ target: { kind: "action_item", id: itemKey }, vote, note })
          }
        />
      ) : null}
    </div>
  );
}

/**
 * Action items grouped by severity tier. Each tier is its own <details>, so
 * they collapse independently and any number can be open at once. All start
 * collapsed — the tier header plus count is the summary a reviewer scans
 * first, and must_fix is the tier that can change the label.
 */
function SeverityGroups({
  items,
  onFeedback,
  className,
}: {
  items: PreTrialFinding[];
  onFeedback?: (r: FeedbackRecord) => void;
  className?: string;
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
              {group.meta.labelEffect}
            </span>
          </summary>

          {/* Items sit directly in the group body, split by rules rather
              than nested cards. */}
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

/* ------------------------------------------------------------------ report */

export function QaAssessmentReport({
  classification,
  subtype,
  rootCause,
  recommendation,
  evidence,
  actionItems,
  duration,
  raw,
  onFeedback,
  className,
}: QaAssessmentReportProps) {
  const token = VERDICT_TOKENS[classification] ?? FALLBACK_TOKEN;
  const Icon = token.icon;
  const items = actionItems ?? [];
  const mustFix = items.filter((i) => i.tier === "must_fix").length;
  const showEvidenceCollapsed = Boolean(
    rootCause && evidence && rootCause !== evidence,
  );

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
              onFeedback({ target: { kind: "verdict", classification }, vote, note })
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
          showEvidenceCollapsed ? (
            /* Collapsed on mount — evidence is backup detail, never the lead. */
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
          ) : (
            <AnalysisProse
              text={evidence}
              className="text-muted-foreground/90 mt-3"
            />
          )
        ) : null}
      </div>
    </article>
  );
}
