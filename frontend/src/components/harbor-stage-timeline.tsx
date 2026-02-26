import { Check, Clock, AlertCircle } from "lucide-react";

interface HarborStageTimelineProps {
  currentStage: string | null | undefined;
  status: string;
  isFailure?: boolean;
}

interface StageInfo {
  id: string;
  label: string;
}

const HARBOR_STAGES: StageInfo[] = [
  {
    id: "starting",
    label: "Initializing",
  },
  {
    id: "trial_started",
    label: "Trial Started",
  },
  {
    id: "environment_setup",
    label: "Environment Setup",
  },
  {
    id: "agent_running",
    label: "Agent Running",
  },
  {
    id: "verification",
    label: "Verification",
  },
  { id: "completed", label: "Completed" },
];

export function HarborStageTimeline({
  currentStage,
  status,
  isFailure = false,
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

        return (
          <div key={stage.id} className="flex gap-2">
            {/* Left column: indicator + line */}
            <div className="flex flex-col items-center">
              {/* Stage indicator */}
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

              {/* Connecting line */}
              {!isLast && (
                <div
                  className={`w-0.5 h-9 ${isCompleted ? completedLine : "bg-muted"}`}
                />
              )}
            </div>

            {/* Stage info */}
            <div className="flex-1 pt-0 pb-4">
              <p
                className={`text-sm font-medium ${
                  isCompleted || isCurrent
                    ? "text-foreground"
                    : "text-muted-foreground"
                }`}
              >
                {isTerminalFailed && stage.id === "completed"
                  ? "Completed (Failed)"
                  : stage.label}
              </p>
            </div>
          </div>
        );
      })}

      {/* Show cancelled or cleanup stage if applicable */}
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
            <p className="text-sm font-medium text-foreground">
              {isCancelled ? "Cancelled" : "Cleanup"}
            </p>
            <p className="text-xs text-muted-foreground">
              {isCancelled ? "Trial was cancelled" : "Cleaning up resources"}
            </p>
          </div>
        </div>
      )}
    </div>
  );
}
