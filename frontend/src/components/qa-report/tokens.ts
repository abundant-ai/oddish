import {
  CircleCheck,
  ShieldAlert,
  TriangleAlert,
  Unplug,
} from "lucide-react";

// Colour carries good/bad, not success/failure: green = valid signal,
// amber = task needs fixing, red = false positive, orange = infrastructure.
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

export const FALLBACK_TOKEN: VerdictToken = {
  icon: TriangleAlert,
  accent: "text-muted-foreground",
  card: "border-border bg-muted/20",
  chip: "border-border",
};

export const TIER_ORDER = ["must_fix", "should_fix", "optional"] as const;

export const TIER_META: Record<string, { label: string; labelEffect: string }> =
  {
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

// Severity is deliberately not hue-coded: only must_fix gets colour, because
// only must_fix can change the label, so a severity badge can never be
// mistaken for a verdict.
export const TIER_BADGE: Record<string, string> = {
  must_fix: "bg-destructive text-destructive-foreground",
  should_fix: "border-foreground/25 bg-foreground/10 text-foreground border",
  optional: "border-border text-muted-foreground border bg-transparent",
};
