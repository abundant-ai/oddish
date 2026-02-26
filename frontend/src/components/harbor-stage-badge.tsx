import { Badge, type BadgeProps } from "@/components/ui/badge";
import { formatHarborStage } from "@/lib/api";

interface HarborStageBadgeProps {
  stage: string | null | undefined;
  className?: string;
}

const STAGE_VARIANTS: Record<string, NonNullable<BadgeProps["variant"]>> = {
  starting: "harborStarting",
  trial_started: "harborTrialStarted",
  environment_setup: "harborEnvironmentSetup",
  agent_running: "harborAgentRunning",
  verification: "harborVerification",
  completed: "harborCompleted",
  cleanup: "harborCleanup",
  cancelled: "harborCancelled",
};

export function HarborStageBadge({ stage, className }: HarborStageBadgeProps) {
  if (!stage) {
    return (
      <Badge variant="harborStarting" className={className}>
        Pending
      </Badge>
    );
  }

  const variant = STAGE_VARIANTS[stage] ?? "harborCleanup";

  return (
    <Badge variant={variant} className={className}>
      {formatHarborStage(stage)}
    </Badge>
  );
}
