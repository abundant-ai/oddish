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
 *
 * `matrixClass` uses the Paper palette tokens registered in globals.css
 * (`--paper-pass`, `--paper-fail`, etc.) so tiles stay in sync with the
 * rest of the experiment results page. Terminal outcomes (pass/fail/
 * partial) get saturated fills; error/queued/running/pending use a
 * light tinted background with a colored hairline border.
 *
 * The other class variants (`badgeClass`, `bracketClass`,
 * `panelBadgeClass`) stay on Tailwind semantic colors so the rest of
 * the app (dashboard, task browser, drawers) is unchanged.
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
    matrixClass: "bg-paper-pass text-white border-paper-pass hover:opacity-90",
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
      "bg-paper-partial text-slate-950 border-paper-partial hover:opacity-90",
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
    matrixClass: "bg-paper-fail text-white border-paper-fail hover:opacity-90",
    bracketClass: "bg-red-600 text-white",
    panelBadgeClass: "bg-red-500/20 text-red-400 border-red-500/50",
  },
  "harness-error": {
    icon: Ban,
    label: "ERROR",
    shortLabel: "Error",
    symbol: "⊘",
    description: "Harness or infrastructure error",
    badgeClass:
      "bg-yellow-500/90 text-gray-900 border-yellow-400 hover:bg-yellow-600",
    matrixClass:
      "bg-yellow-500 text-slate-900 border-yellow-500 hover:bg-yellow-500/90!",
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
    matrixClass:
      "bg-paper-bg-2 text-paper-ink-3 border-paper-line hover:opacity-90",
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
      "bg-[color:var(--paper-queued-bg)] text-paper-queued border-[color:color-mix(in_oklch,var(--paper-queued),transparent_70%)] hover:opacity-90",
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
      "bg-[color:var(--paper-running-bg)] text-paper-running border-[color:color-mix(in_oklch,var(--paper-running),transparent_70%)] animate-pulse hover:opacity-90",
    bracketClass: "bg-blue-500 text-white animate-pulse",
    panelBadgeClass: "bg-blue-500/20 text-blue-400 border-blue-500/50",
  },
};

/**
 * Shared size for the colored "glyph" box that wraps a StatusIcon. Used by
 * the matrix-cell trial buttons, the toolbar legend filter chips, the
 * anatomy demo sample cell, and the trial-page per-trial navigator so
 * every place that shows a single trial outcome renders as the same
 * visual primitive — change this in one place and they all move together.
 *
 * The `!` on `size-2.5` is load-bearing: when the glyph box is nested
 * inside a `<Button>` (legend filter chips, trial navigator), the
 * button's cva base applies `[&_svg]:size-4` to every descendant svg
 * with equal specificity, and cascade order is unpredictable. The
 * `!important` pins the svg to 10px wherever the box is rendered.
 */
export const STATUS_GLYPH_BOX =
  "h-[18px] w-[22px] shrink-0 rounded-[4px] border [&_svg]:size-2.5!";

function hasRewardValue(reward: number | null | undefined): reward is number {
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

function getRewardMatrixStatus(reward: number): MatrixStatus {
  if (reward === 1) return "pass";
  if (reward === 0) return "fail";
  return "partial";
}

function isPartialReward(reward: number | null | undefined): reward is number {
  return hasRewardValue(reward) && reward > 0 && reward < 1;
}

/**
 * Warm partial-reward ramp.
 *
 * Saturated red (0) → orange → olive → forest green (1), with white text.
 * Matches the reference design — high-saturation oklch fills so the score
 * number reads at a glance in the matrix.
 */
function partialWarmRamp(reward: number): {
  bg: string;
  fg: string;
  border: string;
} {
  const s = Math.max(0, Math.min(1, reward));
  const hue = 25 + s * 115;
  const chroma = 0.16 + Math.abs(s - 0.5) * -0.02 + 0.02;
  const light = s < 0.5 ? 62 - s * 8 : 54 + (s - 0.5) * 4;
  const bg = `oklch(${light.toFixed(2)}% ${chroma.toFixed(3)} ${hue.toFixed(1)})`;
  return { bg, fg: "#fff", border: bg };
}

export function getRewardStyle(
  reward: number | null | undefined,
  variant: "matrix" | "badge" | "panel" = "matrix",
): CSSProperties | undefined {
  if (!isPartialReward(reward)) return undefined;
  const { bg, fg, border } = partialWarmRamp(reward);
  if (variant === "matrix") {
    return {
      backgroundColor: bg,
      borderColor: border,
      color: fg,
      boxShadow: "inset 0 -1px 0 rgba(0,0,0,0.12)",
    };
  }
  if (variant === "badge") {
    return {
      backgroundColor: `color-mix(in oklch, ${bg}, transparent 75%)`,
      borderColor: `color-mix(in oklch, ${bg}, transparent 45%)`,
      color: bg,
    };
  }
  return {
    backgroundColor: `color-mix(in oklch, ${bg}, transparent 85%)`,
    borderColor: `color-mix(in oklch, ${bg}, transparent 65%)`,
  };
}

/**
 * Get the matrix status from a trial's status, reward, and error message.
 *
 * Classification rule of thumb: if the verifier ran to completion and
 * produced a numeric reward, that reward IS the truth — even if the agent
 * itself exited non-zero (e.g. a Claude/Gemini CLI returned exit 1 because
 * the model refused the prompt under its Usage Policy, or hit an internal
 * soft-timeout). Those are *agent* failures, not *harness* failures, and
 * should land in the pass/partial/fail bucket based on the reward, not be
 * lumped in with "container never came up" infra errors.
 *
 * harness-error (yellow) is reserved for cases where the trial machinery
 * itself didn't get a clean read on the agent's work — typically:
 *   - status === "failed" with no reward (worker crashed, image build
 *     failed, container died before verification ran)
 *   - status === "success" but somehow no reward came back
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

  // The verifier ran and produced a reward — trust it. Any error_message
  // attached at this point describes something that happened *during* the
  // agent run (refusal, non-zero exit, soft timeout) but didn't prevent
  // verification from completing.
  if (trialStatus === "success" && hasReward) {
    return getRewardMatrixStatus(reward);
  }

  // Agent timeout that nonetheless produced a reward: same idea — trust
  // the reward even though the trial is marked failed.
  if (trialStatus === "failed" && isAgentTimeout && hasReward) {
    return getRewardMatrixStatus(reward);
  }

  // Anything else with an error message AND no usable reward = real
  // harness/infra problem. Examples: image build failures, worker
  // heartbeat stalled, container OOM before verifier ran, agent binary
  // not found.
  if (errorMessage) {
    return "harness-error";
  }

  // Failed without an error message (rare — typically cancelled): still
  // harness-error since we have no reward to lean on.
  if (trialStatus === "failed") {
    return "harness-error";
  }

  // Success execution but no reward yet — still pending result.
  if (trialStatus === "success") {
    return "pending";
  }

  // Queued = waiting in queue. Backend still emits "pending" (it's the
  // default JobStatus for freshly-created trials that haven't been
  // claimed yet); UI-side we fold it into "queued" since the distinction
  // isn't meaningful to users. The backend enum is slated for deprecation
  // -- see oddish/db/models.py.
  if (trialStatus === "queued" || trialStatus === "pending") {
    return "queued";
  }

  // Running = currently executing
  if (trialStatus === "running") {
    return "running";
  }

  // Any other status (retrying) = pending
  return "pending";
}
