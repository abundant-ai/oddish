import { Check, Clock, AlertCircle } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

interface PhaseTimingEntry {
  started_at: string;
  finished_at: string;
  duration_sec: number;
}

interface PhaseTiming {
  environment_setup?: PhaseTimingEntry;
  agent_setup?: PhaseTimingEntry;
  agent_execution?: PhaseTimingEntry;
  verifier?: PhaseTimingEntry;
}

interface HarborStageTimelineProps {
  currentStage: string | null | undefined;
  status: string;
  isFailure?: boolean;
  onStageClick?: (stageId: string) => void;
  phaseTiming?: PhaseTiming | null;
  startedAt?: string | null;
  finishedAt?: string | null;
}

interface StageInfo {
  id: string;
  label: string;
}

const HARBOR_STAGES: StageInfo[] = [
  { id: "starting", label: "Initializing" },
  { id: "trial_started", label: "Trial Started" },
  { id: "environment_setup", label: "Environment Setup" },
  { id: "agent_running", label: "Agent Running" },
  { id: "verification", label: "Verification" },
  { id: "completed", label: "Completed" },
];

function formatDuration(sec: number): string {
  if (sec < 1) return `${Math.round(sec * 1000)}ms`;
  if (sec < 60) return `${Math.round(sec)}s`;
  const min = Math.floor(sec / 60);
  const remainSec = Math.round(sec % 60);
  if (min < 60) return remainSec > 0 ? `${min}m ${remainSec}s` : `${min}m`;
  const hr = Math.floor(min / 60);
  const remainMin = min % 60;
  return remainMin > 0 ? `${hr}h ${remainMin}m` : `${hr}h`;
}

function getStageDuration(
  stageId: string,
  phaseTiming: PhaseTiming | null | undefined,
  startedAt: string | null | undefined,
  finishedAt: string | null | undefined,
): string | null {
  if (!phaseTiming) return null;

  switch (stageId) {
    case "environment_setup": {
      const envSec = phaseTiming.environment_setup?.duration_sec ?? 0;
      const setupSec = phaseTiming.agent_setup?.duration_sec ?? 0;
      const total = envSec + setupSec;
      return total > 0 ? formatDuration(total) : null;
    }
    case "agent_running":
      return phaseTiming.agent_execution?.duration_sec != null
        ? formatDuration(phaseTiming.agent_execution.duration_sec)
        : null;
    case "verification":
      return phaseTiming.verifier?.duration_sec != null
        ? formatDuration(phaseTiming.verifier.duration_sec)
        : null;
    case "completed": {
      if (!startedAt || !finishedAt) return null;
      const totalSec =
        (new Date(finishedAt).getTime() - new Date(startedAt).getTime()) / 1000;
      return totalSec > 0 ? formatDuration(totalSec) : null;
    }
    default:
      return null;
  }
}

export function HarborStageTimeline({
  currentStage,
  status,
  isFailure = false,
  onStageClick,
  phaseTiming,
  startedAt,
  finishedAt,
}: HarborStageTimelineProps) {
  if (!currentStage) {
    return null;
  }

  const currentIndex = HARBOR_STAGES.findIndex((s) => s.id === currentStage);
  const isFailed = status === "failed";
  const isCancelled = currentStage === "cancelled";

  return (
    <div className="space-y-0">
      {HARBOR_STAGES.map((stage, index) => {
        const isCompleted =
          index < currentIndex ||
          (index === currentIndex && currentStage === "completed");
        const isCurrent =
          index === currentIndex && currentStage !== "completed";
        const isLast = index === HARBOR_STAGES.length - 1;
        const isTerminalFailed = isFailure && currentStage === "completed";
        const completedTone = isTerminalFailed
          ? "text-red-400"
          : "text-green-400";
        const completedBg = isTerminalFailed
          ? "bg-red-500/20 border-red-500"
          : "bg-green-500/20 border-green-500";
        const completedLine = isTerminalFailed ? "bg-red-500" : "bg-green-500";

        const duration =
          isCompleted || isCurrent
            ? getStageDuration(stage.id, phaseTiming, startedAt, finishedAt)
            : null;

        const stageLabel =
          isTerminalFailed && stage.id === "completed"
            ? "Completed (Failed)"
            : stage.label;

        return (
          <div key={stage.id} className="flex gap-2">
            {/* Left column: indicator + line */}
            <div className="flex flex-col items-center">
              <div className="relative z-10 flex-shrink-0">
                {isCompleted ? (
                  <div
                    className={`w-5 h-5 rounded-full border flex items-center justify-center ${completedBg}`}
                  >
                    {isTerminalFailed ? (
                      <AlertCircle className="h-2.5 w-2.5 text-red-400" />
                    ) : (
                      <Check className={`h-2.5 w-2.5 ${completedTone}`} />
                    )}
                  </div>
                ) : isCurrent ? (
                  <div className="w-5 h-5 rounded-full bg-blue-500/20 border border-blue-500 flex items-center justify-center">
                    {isFailed || isCancelled ? (
                      <AlertCircle className="h-2.5 w-2.5 text-red-400" />
                    ) : (
                      <Clock className="h-2.5 w-2.5 text-blue-400 animate-pulse" />
                    )}
                  </div>
                ) : (
                  <div className="w-5 h-5 rounded-full bg-muted border border-muted-foreground/20" />
                )}
              </div>

              {!isLast && (
                <div
                  className={`w-0.5 h-6 ${isCompleted ? completedLine : "bg-muted"}`}
                />
              )}
            </div>

            {/* Stage info */}
            <div className="flex-1 flex items-baseline justify-between gap-2 pb-2.5">
              {(isCompleted || isCurrent) && onStageClick ? (
                <Button
                  type="button"
                  variant="ghost"
                  onClick={() => onStageClick(stage.id)}
                  className={cn(
                    "h-auto -mx-1.5 -my-0.5 px-1.5 py-0.5 text-left text-xs font-medium leading-normal hover:bg-muted hover:underline hover:underline-offset-2",
                    isCompleted || isCurrent
                      ? "text-foreground"
                      : "text-muted-foreground",
                  )}
                >
                  {stageLabel}
                </Button>
              ) : (
                <p
                  className={cn(
                    "text-xs font-medium",
                    isCompleted || isCurrent
                      ? "text-foreground"
                      : "text-muted-foreground",
                  )}
                >
                  {stageLabel}
                </p>
              )}

              {duration && (
                <span className="text-[10px] tabular-nums text-muted-foreground shrink-0">
                  {duration}
                </span>
              )}
            </div>
          </div>
        );
      })}

      {(isCancelled || currentStage === "cleanup") && (
        <div className="flex gap-2">
          <div className="relative z-10 flex-shrink-0">
            <div
              className={`w-5 h-5 rounded-full border flex items-center justify-center ${
                isCancelled
                  ? "bg-red-500/20 border-red-500"
                  : "bg-gray-500/20 border-gray-500"
              }`}
            >
              <AlertCircle
                className={`h-2.5 w-2.5 ${isCancelled ? "text-red-400" : "text-gray-400"}`}
              />
            </div>
          </div>
          <div className="flex-1 pt-0">
            <p className="text-xs font-medium text-foreground">
              {isCancelled ? "Cancelled" : "Cleanup"}
            </p>
            <p className="text-[10px] text-muted-foreground">
              {isCancelled ? "Trial was cancelled" : "Cleaning up resources"}
            </p>
          </div>
        </div>
      )}
    </div>
  );
}
