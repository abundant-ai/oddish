import {
  CircleDashed,
  CheckCircle2,
  XCircle,
  Ban,
  Loader2,
  type LucideIcon,
} from "lucide-react";
import type { CSSProperties } from "react";

/**
 * Trial status types that map to visual states in the UI.
 * These are derived from trial.status and trial.reward values.
 */
export type MatrixStatus =
  | "pass"
  | "partial"
  | "fail"
  | "harness-error"
  | "pending"
  | "queued"
  | "running";

/**
 * Status configuration for consistent styling across the UI.
 * Inspired by sauron's status-config.ts but simplified for oddish.
 */
export const STATUS_CONFIG: Record<
  MatrixStatus,
  {
    icon: LucideIcon;
    label: string;
    shortLabel: string;
    symbol: string;
    description: string;
    badgeClass: string;
    matrixClass: string;
    bracketClass: string;
    panelBadgeClass: string;
  }
> = {
  pass: {
    icon: CheckCircle2,
    label: "PASS",
    shortLabel: "Pass",
    symbol: "✓",
    description: "Task completed successfully",
    badgeClass:
      "bg-emerald-500/90 text-white border-emerald-400 hover:bg-emerald-600",
    matrixClass:
      "bg-emerald-500 text-white border-emerald-500 hover:!bg-emerald-500/90",
    bracketClass: "bg-emerald-600 text-white",
    panelBadgeClass: "bg-emerald-500/20 text-emerald-400 border-emerald-500/50",
  },
  partial: {
    icon: CircleDashed,
    label: "PARTIAL",
    shortLabel: "Partial",
    symbol: "~",
    description: "Task earned partial credit",
    badgeClass:
      "bg-amber-500/90 text-slate-950 border-amber-400 hover:bg-amber-600",
    matrixClass:
      "bg-amber-500 text-slate-950 border-amber-500 hover:!bg-amber-500/90",
    bracketClass: "bg-amber-500 text-slate-950",
    panelBadgeClass: "bg-amber-500/20 text-amber-400 border-amber-500/50",
  },
  fail: {
    icon: XCircle,
    label: "FAIL",
    shortLabel: "Fail",
    symbol: "✗",
    description: "Task did not pass",
    badgeClass: "bg-red-600/90 text-white border-red-500 hover:bg-red-700",
    matrixClass: "bg-red-500 text-white border-red-500 hover:!bg-red-500/90",
    bracketClass: "bg-red-600 text-white",
    panelBadgeClass: "bg-red-500/20 text-red-400 border-red-500/50",
  },
  "harness-error": {
    icon: Ban,
    label: "ERROR",
    shortLabel: "Harness error",
    symbol: "⊘",
    description: "Harness or infrastructure error",
    badgeClass:
      "bg-yellow-500/90 text-gray-900 border-yellow-400 hover:bg-yellow-600",
    matrixClass:
      "bg-yellow-500 text-slate-900 border-yellow-500 hover:!bg-yellow-500/90",
    bracketClass: "bg-yellow-500 text-gray-900",
    panelBadgeClass: "bg-yellow-500/20 text-yellow-400 border-yellow-500/50",
  },
  pending: {
    icon: Loader2,
    label: "PENDING",
    shortLabel: "Pending",
    symbol: "◌",
    description: "Waiting to be queued",
    badgeClass: "bg-gray-500/50 text-gray-300 border-gray-400 animate-pulse",
    matrixClass: "bg-gray-500 text-white border-gray-500 hover:!bg-gray-500/90",
    bracketClass: "bg-gray-500/50 text-gray-300 animate-pulse",
    panelBadgeClass: "bg-gray-500/20 text-gray-400 border-gray-500/50",
  },
  queued: {
    icon: Loader2,
    label: "QUEUED",
    shortLabel: "Queued",
    symbol: "⟳",
    description: "Queued for execution",
    badgeClass: "bg-purple-500/90 text-white border-purple-400",
    matrixClass:
      "bg-purple-500 text-white border-purple-500 hover:!bg-purple-500/90",
    bracketClass: "bg-purple-500 text-white",
    panelBadgeClass: "bg-purple-500/20 text-purple-400 border-purple-500/50",
  },
  running: {
    icon: Loader2,
    label: "RUNNING",
    shortLabel: "Running",
    symbol: "⟳",
    description: "Currently executing",
    badgeClass: "bg-blue-500/90 text-white border-blue-400 animate-pulse",
    matrixClass:
      "bg-blue-500 text-white border-blue-500 animate-pulse hover:!bg-blue-500/90",
    bracketClass: "bg-blue-500 text-white animate-pulse",
    panelBadgeClass: "bg-blue-500/20 text-blue-400 border-blue-500/50",
  },
};

export function hasRewardValue(
  reward: number | null | undefined,
): reward is number {
  return typeof reward === "number" && Number.isFinite(reward);
}

export function formatRewardValue(
  reward: number | null | undefined,
  digits = 2,
): string {
  if (!hasRewardValue(reward)) return "—";
  return reward.toFixed(digits);
}

export function formatRewardPercent(
  reward: number | null | undefined,
  digits = 0,
): string {
  if (!hasRewardValue(reward)) return "—";
  return `${(reward * 100).toFixed(digits)}%`;
}

export function formatPartialRewardBadgeValue(
  reward: number | null | undefined,
): string {
  if (!hasRewardValue(reward)) return "—";
  const fixed = reward.toFixed(2);
  if (fixed.startsWith("0.")) return fixed.slice(1);
  if (fixed.startsWith("-0.")) return `-${fixed.slice(2)}`;
  return fixed;
}

export function getRewardMatrixStatus(reward: number): MatrixStatus {
  if (reward === 1) return "pass";
  if (reward === 0) return "fail";
  return "partial";
}

function isPartialReward(reward: number | null | undefined): reward is number {
  return hasRewardValue(reward) && reward > 0 && reward < 1;
}

type RgbColor = {
  r: number;
  g: number;
  b: number;
};

const FAIL_BG: RgbColor = { r: 239, g: 68, b: 68 }; // red-500
const PASS_BG: RgbColor = { r: 16, g: 185, b: 129 }; // emerald-500
const FAIL_BORDER: RgbColor = { r: 220, g: 38, b: 38 }; // red-600
const PASS_BORDER: RgbColor = { r: 5, g: 150, b: 105 }; // emerald-600

function clampChannel(value: number): number {
  return Math.max(0, Math.min(255, Math.round(value)));
}

function interpolateRgb(
  from: RgbColor,
  to: RgbColor,
  weight: number,
): RgbColor {
  const clamped = Math.max(0, Math.min(1, weight));
  return {
    r: clampChannel(from.r + (to.r - from.r) * clamped),
    g: clampChannel(from.g + (to.g - from.g) * clamped),
    b: clampChannel(from.b + (to.b - from.b) * clamped),
  };
}

function rgbCss(color: RgbColor, alpha?: number): string {
  if (alpha == null) {
    return `rgb(${color.r} ${color.g} ${color.b})`;
  }
  return `rgb(${color.r} ${color.g} ${color.b} / ${alpha})`;
}

function getPartialRewardColors(reward: number): {
  background: RgbColor;
  border: RgbColor;
} {
  return {
    // Interpolate directly between the existing fail/pass palette so
    // partials feel like "between fail and pass" rather than a brighter
    // independent spectrum.
    background: interpolateRgb(FAIL_BG, PASS_BG, reward),
    border: interpolateRgb(FAIL_BORDER, PASS_BORDER, reward),
  };
}

export function getRewardStyle(
  reward: number | null | undefined,
  variant: "matrix" | "badge" | "panel" = "matrix",
): CSSProperties | undefined {
  if (!isPartialReward(reward)) return undefined;
  const { background, border } = getPartialRewardColors(reward);
  if (variant === "matrix") {
    return {
      backgroundColor: rgbCss(background),
      borderColor: rgbCss(border),
      color: "white",
    };
  }
  if (variant === "badge") {
    return {
      backgroundColor: rgbCss(background, 0.18),
      borderColor: rgbCss(border, 0.45),
      color: rgbCss(background),
    };
  }
  return {
    backgroundColor: rgbCss(background, 0.12),
    borderColor: rgbCss(border, 0.3),
  };
}

/**
 * Get the matrix status from a trial's status, reward, and error message.
 */
export function getMatrixStatus(
  trialStatus: string,
  reward: number | null | undefined,
  errorMessage?: string | null,
): MatrixStatus {
  const isAgentTimeout =
    !!errorMessage &&
    (errorMessage.includes("AgentTimeoutError") ||
      errorMessage.includes("Agent execution timed out"));
  const hasReward = hasRewardValue(reward);

  // If there's an error message, treat as harness error regardless of status,
  // except for agent timeouts that still produced a reward.
  if (errorMessage && !(isAgentTimeout && hasReward)) {
    return "harness-error";
  }

  // Failed execution = harness error
  if (trialStatus === "failed") {
    if (isAgentTimeout && hasReward) {
      return getRewardMatrixStatus(reward);
    }
    return "harness-error";
  }

  // Success execution - check reward
  if (trialStatus === "success") {
    if (hasReward) return getRewardMatrixStatus(reward);
    // No reward yet (null/undefined) - still pending result
    return "pending";
  }

  // Queued = waiting in queue
  if (trialStatus === "queued") {
    return "queued";
  }

  // Running = currently executing
  if (trialStatus === "running") {
    return "running";
  }

  // Any other status (pending, retrying) = pending
  return "pending";
}
