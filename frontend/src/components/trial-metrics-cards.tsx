"use client";

import { Card, CardContent } from "@/components/ui/card";
import { Trophy, XCircle, AlertCircle, Loader2 } from "lucide-react";
import { cn } from "@/lib/utils";
import type { Trial } from "@/lib/types";
import { getMatrixStatus, type MatrixStatus } from "@/lib/status-config";

interface TrialMetricsCardsProps {
  trial: Trial;
}

// Status styling for outcome card
const OUTCOME_STYLES: Record<
  MatrixStatus,
  { bg: string; border: string; icon: typeof Trophy; iconColor: string }
> = {
  pass: {
    bg: "bg-emerald-500/10",
    border: "border-emerald-500/30",
    icon: Trophy,
    iconColor: "text-emerald-500",
  },
  fail: {
    bg: "bg-red-500/10",
    border: "border-red-500/30",
    icon: XCircle,
    iconColor: "text-red-500",
  },
  "harness-error": {
    bg: "bg-yellow-500/10",
    border: "border-yellow-500/30",
    icon: AlertCircle,
    iconColor: "text-yellow-500",
  },
  pending: {
    bg: "bg-gray-500/10",
    border: "border-gray-500/30",
    icon: Loader2,
    iconColor: "text-gray-500",
  },
  queued: {
    bg: "bg-purple-500/10",
    border: "border-purple-500/30",
    icon: Loader2,
    iconColor: "text-purple-500",
  },
  running: {
    bg: "bg-blue-500/10",
    border: "border-blue-500/30",
    icon: Loader2,
    iconColor: "text-blue-500",
  },
};

export function TrialMetricsCards({ trial }: TrialMetricsCardsProps) {
  const status = getMatrixStatus(
    trial.status,
    trial.reward,
    trial.error_message,
  );
  const outcomeStyle = OUTCOME_STYLES[status];
  const OutcomeIcon = outcomeStyle.icon;

  return (
    <div className="grid grid-cols-1 gap-3 h-full">
      {/* Outcome Card */}
      <Card
        className={cn("border h-full", outcomeStyle.border, outcomeStyle.bg)}
      >
        <CardContent className="py-2 px-3">
          <div className="flex items-center gap-2.5">
            <OutcomeIcon
              className={cn(
                "h-6 w-6",
                outcomeStyle.iconColor,
                (status === "pending" ||
                  status === "queued" ||
                  status === "running") &&
                  "animate-spin",
              )}
            />
            <div className="flex-1 min-w-0">
              <div className="text-[9px] uppercase tracking-wider text-muted-foreground">
                Reward
              </div>
              <div className="flex items-baseline gap-2">
                <span className="text-xl font-bold font-mono">
                  {trial.reward !== null ? trial.reward : "—"}
                </span>
                <span className="text-[10px] text-muted-foreground capitalize">
                  {status.replace("-", " ")}
                </span>
              </div>
              {trial.error_message && (
                <div className="text-[9px] text-red-500 truncate mt-0.5">
                  {trial.error_message.split("\n")[0]?.slice(0, 50)}...
                </div>
              )}
            </div>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
