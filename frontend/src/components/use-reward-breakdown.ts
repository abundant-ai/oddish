"use client";

import { useEffect, useMemo, useState } from "react";
import type { Trial } from "@/lib/types";
import {
  embeddedRewardBreakdown,
  embeddedRewardsMap,
  parseRewardDetailsDocument,
  parseRewardsMap,
  type RewardBreakdown,
  type RewardsMap,
} from "@/lib/reward-kit";

const TERMINAL_STATUSES = ["success", "failed", "cancelled", "skipped"];

function encodeFilePath(path: string): string {
  return path.split("/").map(encodeURIComponent).join("/");
}

export interface RewardBreakdownState {
  rewards: RewardsMap | null;
  breakdown: RewardBreakdown | null;
  /** True while the full details document is being fetched. */
  loadingDetails: boolean;
}

/**
 * A trial's RewardKit output: the named rewards map and the per-criterion
 * breakdown.
 *
 * Embedded copies (`result._rewards` / `result._reward_details`) render
 * immediately. The full `verifier/reward-details.json` is fetched through the
 * already-scoped trial files API when the embedded summary is missing
 * (imports, historical trials) or truncated — same pattern as the CTRF
 * fallback in `useVerifierSummary`; never an unscoped artifact lookup.
 */
export function useRewardBreakdown(
  trial: Trial | null,
  apiBaseUrl: string,
  enabled: boolean
): RewardBreakdownState {
  const result = trial?.result;
  const embeddedMap = useMemo(() => embeddedRewardsMap(result), [result]);
  const embedded = useMemo(() => embeddedRewardBreakdown(result), [result]);
  const trialId = trial?.id ?? null;
  const shouldLoad =
    enabled &&
    trial !== null &&
    (embedded === null || embedded.truncated || embeddedMap === null) &&
    TERMINAL_STATUSES.includes(trial.status);
  const artifactKey = `${apiBaseUrl}\0${trialId ?? ""}`;
  const [artifact, setArtifact] = useState<{
    key: string;
    rewards: RewardsMap | null;
    breakdown: RewardBreakdown | null;
    loading: boolean;
  }>({ key: artifactKey, rewards: null, breakdown: null, loading: false });

  useEffect(() => {
    setArtifact({
      key: artifactKey,
      rewards: null,
      breakdown: null,
      loading: shouldLoad,
    });
    if (!shouldLoad || trialId === null) return;

    const controller = new AbortController();
    const filesBase = `${apiBaseUrl.replace(/\/$/, "")}/trials/${encodeURIComponent(trialId)}/files`;

    async function loadArtifacts() {
      let rewards: RewardsMap | null = null;
      let breakdown: RewardBreakdown | null = null;
      try {
        const listingResponse = await fetch(
          `${filesBase}?recursive=true&presign=false&limit=1000`,
          { signal: controller.signal }
        );
        if (!listingResponse.ok) return;
        const listing = (await listingResponse.json()) as {
          files?: { path?: string }[];
        };
        const paths = (listing.files ?? [])
          .map((file) => file.path)
          .filter((path): path is string => typeof path === "string");
        const findVerifierFile = (name: string) =>
          paths.find(
            (path) =>
              path === `verifier/${name}` || path.endsWith(`/verifier/${name}`)
          );

        const detailsPath = findVerifierFile("reward-details.json");
        if (detailsPath) {
          const response = await fetch(
            `${filesBase}/${encodeFilePath(detailsPath)}`,
            { signal: controller.signal }
          );
          if (response.ok) {
            const parsed = parseRewardDetailsDocument(await response.json());
            if (parsed) breakdown = { ...parsed, detailsPath };
          }
        }

        // reward.json only needs its own fetch when the embedded map is
        // missing; a full details document already carries every dimension
        // score but not the reward.toml aggregates, which live here.
        const rewardsPath = findVerifierFile("reward.json");
        if (rewardsPath) {
          const response = await fetch(
            `${filesBase}/${encodeFilePath(rewardsPath)}`,
            { signal: controller.signal }
          );
          if (response.ok) {
            rewards = parseRewardsMap(await response.json());
          }
        }
      } catch {
        // Forgiving by design: a missing/broken artifact renders nothing.
      } finally {
        if (!controller.signal.aborted) {
          setArtifact({ key: artifactKey, rewards, breakdown, loading: false });
        }
      }
    }

    void loadArtifacts();
    return () => controller.abort();
  }, [apiBaseUrl, artifactKey, shouldLoad, trialId]);

  const current = artifact.key === artifactKey ? artifact : null;
  return {
    rewards: embeddedMap ?? current?.rewards ?? null,
    // A freshly fetched full document beats a truncated embedded summary.
    breakdown: current?.breakdown ?? embedded ?? null,
    loadingDetails: current?.loading ?? false,
  };
}
