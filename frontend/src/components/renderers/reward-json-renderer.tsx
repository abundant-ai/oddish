"use client";

import { JsonRenderer } from "./json-renderer";
import { RewardBreakdownView } from "@/components/reward-breakdown-view";
import {
  parseRewardDetailsDocument,
  parseRewardsMap,
  type RewardBreakdown,
  type RewardsMap,
} from "@/lib/reward-kit";

/**
 * Renders RewardKit verifier output files as a reward tree instead of raw
 * JSON: `reward.json` (flat named-score map) and `reward-details.json`
 * (per-criterion breakdown with judge reasoning). Anything that doesn't
 * parse as either falls back to the plain JSON view.
 */
export function RewardJsonRenderer({ content }: { content: string }) {
  let parsed: unknown;
  try {
    parsed = JSON.parse(content);
  } catch {
    return <JsonRenderer content={content} />;
  }

  const breakdown: RewardBreakdown | null = parseRewardDetailsDocument(parsed);
  const rewards: RewardsMap | null = breakdown ? null : parseRewardsMap(parsed);

  if (!breakdown && !rewards) {
    return <JsonRenderer content={content} />;
  }

  // A bare reward.json map has no dimension entries to classify against, so
  // every named score renders in the chip row.
  return (
    <div className="p-4">
      <RewardBreakdownView breakdown={breakdown} rewards={rewards} />
    </div>
  );
}
