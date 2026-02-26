import {
  CheckCircle2,
  XCircle,
  Ban,
  Loader2,
  type LucideIcon,
} from "lucide-react";

/**
 * Trial status types that map to visual states in the UI.
 * These are derived from trial.status and trial.reward values.
 */
export type MatrixStatus =
  | "pass"
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
    matrixClass: "bg-emerald-500 text-white border-emerald-500",
    bracketClass: "bg-emerald-600 text-white",
    panelBadgeClass: "bg-emerald-500/20 text-emerald-400 border-emerald-500/50",
  },
  fail: {
    icon: XCircle,
    label: "FAIL",
    shortLabel: "Fail",
    symbol: "✗",
    description: "Task did not pass",
    badgeClass: "bg-red-600/90 text-white border-red-500 hover:bg-red-700",
    matrixClass: "bg-red-500 text-white border-red-500",
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
    matrixClass: "bg-yellow-500 text-slate-900 border-yellow-500",
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
    matrixClass: "bg-gray-500 text-white border-gray-500",
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
    matrixClass: "bg-purple-500 text-white border-purple-500",
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
    matrixClass: "bg-blue-500 text-white border-blue-500 animate-pulse",
    bracketClass: "bg-blue-500 text-white animate-pulse",
    panelBadgeClass: "bg-blue-500/20 text-blue-400 border-blue-500/50",
  },
};

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
  const hasReward = reward === 0 || reward === 1;

  // If there's an error message, treat as harness error regardless of status,
  // except for agent timeouts that still produced a reward.
  if (errorMessage && !(isAgentTimeout && hasReward)) {
    return "harness-error";
  }

  // Failed execution = harness error
  if (trialStatus === "failed") {
    if (isAgentTimeout && hasReward) {
      return reward === 1 ? "pass" : "fail";
    }
    return "harness-error";
  }

  // Success execution - check reward
  if (trialStatus === "success") {
    if (reward === 1) return "pass";
    if (reward === 0) return "fail";
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
