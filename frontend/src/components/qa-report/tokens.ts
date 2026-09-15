import { CircleCheck, ShieldAlert, TriangleAlert, Unplug } from "lucide-react";

// Green = valid evaluation; red = task defect affected evaluation;
// amber = execution could not receive a valid evaluation.
export type VerdictToken = {
  icon: typeof CircleCheck;
  accent: string;
  card: string;
  chip: string;
};

export const VERDICT_TOKENS: Record<string, VerdictToken> = {
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
    accent: "text-red-600 dark:text-red-400",
    card: "border-red-500/30 bg-red-500/5",
    chip: "border-red-500/40",
  },
  BAD_SUCCESS: {
    icon: ShieldAlert,
    accent: "text-red-600 dark:text-red-400",
    card: "border-red-500/35 bg-red-500/5",
    chip: "border-red-500/45",
  },
  HARNESS_ERROR: {
    icon: Unplug,
    accent: "text-amber-600 dark:text-amber-400",
    card: "border-amber-500/30 bg-amber-500/5",
    chip: "border-amber-500/40",
  },
};

export const FALLBACK_TOKEN: VerdictToken = {
  icon: TriangleAlert,
  accent: "text-muted-foreground",
  card: "border-border bg-muted/20",
  chip: "border-border",
};

export const TIER_ORDER = ["must_fix", "optional"] as const;

export const TIER_LABELS: Record<string, string> = {
  must_fix: "Must fix",
  optional: "RECORDED OPTIONAL",
};

// Preserve the recorded severity for historical findings.
export const TIER_BADGE: Record<string, string> = {
  must_fix: "bg-destructive text-destructive-foreground",
  optional: "border-border text-muted-foreground border bg-transparent",
};
