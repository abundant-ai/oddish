"use client";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Loader2 } from "lucide-react";
import type { Trial } from "@/lib/types";
import { useRewardBreakdown } from "@/components/use-reward-breakdown";
import { RewardBreakdownView } from "@/components/reward-breakdown-view";

/**
 * Trial drawer card for RewardKit output: the named rewards next to the
 * headline scalar, and the per-dimension / per-criterion breakdown with judge
 * reasoning. Renders nothing for tasks that report a bare scalar reward, so
 * non-RewardKit trials keep their current drawer.
 */
export function TrialRewardsCard({
  trial,
  apiBaseUrl,
  enabled,
}: {
  trial: Trial | null;
  apiBaseUrl: string;
  enabled: boolean;
}) {
  const { rewards, breakdown, loadingDetails } = useRewardBreakdown(
    trial,
    apiBaseUrl,
    enabled
  );

  if (!rewards && !breakdown) return null;

  return (
    <Card>
      <CardHeader className="px-4 pt-2 pb-1">
        <CardTitle className="text-muted-foreground flex items-center gap-2 text-[11px] font-semibold tracking-wider uppercase">
          <span>Reward Breakdown</span>
          {loadingDetails && (
            <Loader2 className="h-3 w-3 animate-spin opacity-60" />
          )}
        </CardTitle>
      </CardHeader>
      <CardContent className="px-4 pb-3">
        <RewardBreakdownView breakdown={breakdown} rewards={rewards} />
      </CardContent>
    </Card>
  );
}
